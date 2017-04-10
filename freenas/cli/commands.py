#+
# Copyright 2014 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import os
import inspect
import sys
import signal
import select
import readline
import gettext
import platform
import textwrap
import re
import logging
import copy
import getpass
from datetime import datetime
from freenas.cli.parser import Quote, parse, unparse, dump_ast
from freenas.cli.complete import NullComplete, EnumComplete
from freenas.cli.namespace import (
    Command, PipeCommand, CommandException, description,
    SingleItemNamespace, Namespace, FilteringCommand
)
from freenas.cli.output import (
    Table, ValueType, output_less, format_value,
    Sequence, read_value, format_output
)
from freenas.cli.output import Object as output_obj, get_terminal_size
from freenas.cli.descriptions.tasks import translate as translate_task
from freenas.cli.utils import TaskPromise, describe_task_state, parse_timedelta, add_tty_formatting, quote, to_ascii
from freenas.dispatcher.shell import ShellClient
from freenas.utils.url import wrap_address
from urllib.parse import urlparse


if platform.system() != 'Windows':
    import tty
    import termios


t = gettext.translation('freenas-cli', fallback=True)
_ = t.gettext

logger = logging.getLogger('cli.commands')


def create_variable_completer(name, var):
    if var.type == ValueType.BOOLEAN:
        return EnumComplete(name + '=', ['yes', 'no'])

    if var.choices:
        return EnumComplete(name + '=', var.choices)

    return NullComplete(name + '=')


@description("Set configuration variable value")
class SetoptCommand(Command):

    """
    Usage: setopt <variable>=<value>

    Example: setopt debug=yes
             setopt prompt="{path}>"

    Set value of environment variable. Use 'printopt' to display
    available variables and their current values.

    If the value contains any non-alphanumeric characters,
    enclose it between double quotes.
    """

    def run(self, context, args, kwargs, opargs):
        if args:
            raise CommandException(_(
                "Incorrect syntax {0}. For help see 'help <command>'".format(args)
            ))
        if not kwargs:
            raise CommandException(_(
                'Please specify a variable to set. For help see "help <command>"'
            ))

        for k, v in list(kwargs.items()):
            self.variables.set(k, v)

    def complete(self, context, **kwargs):
        return [create_variable_completer(k, v) for k, v in self.variables.get_all()]


@description("Changes the namespace to the specified one")
class ChangeNamespaceCommand(Command):

    """
    Usage: cd namespace/namespace

    Example: cd system/ui
             cd ../..
             cd ../config

    This is basically a navigation command to facilitate unix-like navigation
    """

    def mod_namespaces(self, nslist, prepend=''):
        """Small utility function to append `/` at the end of the namespace name"""
        modded_ns = []
        if prepend and not prepend.endswith('/'):
            prepend += '/'
        for i in nslist:
            modns = copy.copy(i)
            modns.name = '{0}{1}{2}'.format(prepend, modns.name, '/')
            modded_ns.append(modns)
        return modded_ns

    def run(self, context, args, kwargs, opargs):
        if kwargs:
            raise CommandException(_(
                'You cannot specify command like properties in this navigation command'
            ))
        if len(args) > 1:
            raise CommandException(_('Invalid syntax: {0}. For help see "help <command>"'.format(args)))
        elif len(args) == 1:
            path = args[0][0] + args[0][1:].replace('/', ' ').strip()
            return context.ml.process(path)

    def complete(self, context, **kwargs):
        text = kwargs.get('text', None)
        if text is None:
            return []

        # Some defaults
        path = context.ml.path[:]
        prepend = []
        ns = context.ml.path[-1]
        old_ns_list = []

        text = text.strip()

        # hack to make '/..' endings tab complete
        if text.endswith("/.."):
            text += '/'

        # Find the last occurence of '/'
        last_slash = text.rfind('/') if text else -1

        if last_slash > -1:
            if text.startswith('/'):
                prepend = ['/']
                path = path[0]
                ns = context.root_ns
                text = text[1:]
            if last_slash != 0 and last_slash < len(text) - 1:
                # Remove anything after the last slash
                text = text[:last_slash]

        for pseudo_token in filter(lambda x: x.strip() != '', text.split('/')):
            if pseudo_token == '..':
                if old_ns_list:
                    ns = old_ns_list.pop()
                elif isinstance(path, list) and len(path) > 1:
                    del path[-1]
                    ns = path[-1]
                prepend.append('..')
                continue
            for new_ns in ns.namespaces():
                if new_ns != ns and new_ns.name == pseudo_token:
                    old_ns_list.append(ns)
                    ns = new_ns
                    prepend.append(new_ns.name)
                    break
        if prepend and prepend[0] == '/':
            prepend = '/' + '/'.join(prepend[1:])
        else:
            prepend = '/'.join(prepend)
        return self.mod_namespaces(ns.namespaces(), prepend)


@description("Print configuration variable values")
class PrintoptCommand(Command):

    """

    Usage: printopt <variable>

    Example: printopt
             printopt timeout

    Print a list of all environment variables and their values
    or the value of the specified environment variable.
    """

    def run(self, context, args, kwargs, opargs):
        if len(kwargs) > 0:
            raise CommandException(_("Invalid syntax {0}. For help see 'help <command>'".format(kwargs)))

        if len(args) == 0:
            var_dict_list = []
            for k, v in self.variables.get_all_printable():
                var_dict = {
                    'varname': k,
                    'vardescr': self.variables.variable_doc.get(k, ''),
                    'varvalue': v,
                }
                var_dict_list.append(var_dict)
            return Table(var_dict_list, [
                Table.Column('Variable', 'varname', ValueType.STRING),
                Table.Column('Description', 'vardescr', ValueType.STRING),
                Table.Column('Value', 'varvalue')])

        if len(args) == 1:
            try:
                return format_value(self.variables.variables[args[0]])
            except KeyError:
                raise CommandException(_("No such Environment Variable exists"))
        else:
            raise CommandException(_("Invalid syntax {0}. For help see 'help <command>'".format(args)))

    def complete(self, context, **kwargs):
        return [create_variable_completer(k, v) for k, v in self.variables.get_all()]


@description("Print CLI builtin commands")
class BuiltinCommand(Command):
    """

    Usage: builtin <command>

    Example: builtin
             builtin wait

    Print a list of all builtin commands
    or the help for the specified command.
    """

    def run(self, context, args, kwargs, opargs):
        if len(kwargs) > 0:
            raise CommandException(_("Invalid syntax {0}. type builtin or 'builtin <command>'".format(kwargs)))

        if len(args) > 1:
            raise CommandException(_("Invalid syntax {0}. type builtin or 'builtin <command>'".format(kwargs)))

        if len(args) == 0:
            builtin_cmd_dict_list = [
                {"cmd": "/", "description": "Go to the root namespace"},
                {"cmd": "..", "description": "Go up one namespace"},
                {"cmd": "-", "description": "Go back to previous namespace"}
            ]
            filtering_cmd_dict_list = []
            for key, value in context.ml.builtin_commands.items():
                if hasattr(value, 'description') and value.description is not None:
                    description = value.description
                else:
                     description = key
                builtin_cmd_dict = {
                    'cmd': key,
                    'description': description,
                }
                if key in context.ml.pipe_commands:
                    filtering_cmd_dict_list.append(builtin_cmd_dict)
                else:
                    builtin_cmd_dict_list.append(builtin_cmd_dict)

            builtin_cmd_dict_list = sorted(builtin_cmd_dict_list, key=lambda k: k['cmd'])
            filtering_cmd_dict_list = sorted(filtering_cmd_dict_list, key=lambda k: k['cmd'])

            output_seq = Sequence()
            output_seq.append(
                Table(builtin_cmd_dict_list, [
                    Table.Column('Global Command', 'cmd', ValueType.STRING),
                    Table.Column('Description', 'description', ValueType.STRING)
                ]))
            output_seq.append(
                Table(filtering_cmd_dict_list, [
                    Table.Column('Filter Command', 'cmd', ValueType.STRING),
                    Table.Column('Description', 'description', ValueType.STRING)
                ]))
            return output_seq

        if len(args) == 1:
            command_name = args[0]
            default_cmd_help = {
                "/": "Go to the root namespace",
                "..": "Go up one namespace",
                "-": "Go back to previous namespace"
            }
            if command_name in default_cmd_help.keys():
                print(default_cmd_help[command_name])

            elif command_name in context.ml.builtin_commands.keys():
                return inspect.getdoc(context.ml.builtin_commands[command_name]) + '\n'

            else:
                raise CommandException(_("Invalid syntax: '{0}' is not a valid CLI builtin.".format(command_name)))


@description("Save configuration variables to CLI configuration file")
class SaveoptCommand(Command):

    """
    Usage: saveopt
           saveopt <filename>

    Examples:
           saveopt
           saveopt "/root/myclisave.conf"

    Save the current set of environment variables to either the specified filename
    or, when not specified, to "~/.freenascli.conf". To start the CLI with the saved
    variables, type "cli -c filename" from the shell or an SSH session.
    """

    def run(self, context, args, kwargs, opargs):
        if len(args) == 0:
            self.variables.save()
            return "Environment Variables Saved to file: {0}".format(
                context.variables.save_to_file
            )
        if len(args) == 1:
            self.variables.save(args[0])
            return "Environment Variables Saved to file: {0}".format(args[0])
        if len(args) > 1:
            raise CommandException(_(
                "Incorrect syntax: {0}. For help see 'help <command>'".format(args)
            ))


@description("Set environment variable value")
class SetenvCommand(Command):
    """
    Usage: setenv variable=name

    Examples:
           setenv EDITOR=/usr/local/bin/nano


    Sets an environment variable.
    """

    def run(self, context, args, kwargs, opargs):
        for k, v in kwargs.items():
            os.environ[k] = str(v)


@description("Print environment variable values")
class PrintenvCommand(Command):
    """
    Usage: printenv

    Example: printenv

    Prints currently set environment variables and their values.
    """

    def run(self, context, args, kwargs, opargs):
        return Table(
            [{'name': k, 'value': v} for k, v in os.environ.items()],
            [
                Table.Column('Name', 'name'),
                Table.Column('Value', 'value')
            ]
        )


@description("Create aliases for commonly used commands")
class AliasCommand(Command):

    """
    Usage: alias name="CLI command"
           alias

    Example:
           alias us="account user show"

    Map a shortcut to the specified CLI command. You can create an alias for
    anything you can type within the CLI. Once the alias is created, type
    its name to run its associated command. When run without any arguments,
    displays any defined aliases.
    """

    def run(self, context, args, kwargs, opargs):
        if not kwargs:
            data = [{'label': k, 'value': v} for k, v in context.ml.aliases.items()]
            return Table(data, [
                Table.Column('Alias name', 'label'),
                Table.Column('Alias value', 'value')
            ])

        for name, value in kwargs.items():
            context.ml.aliases[name] = value


@description("Remove previously defined alias")
class UnaliasCommand(Command):

    """
    Usage: unalias <name>

    Example:
           unalias us

    Remove the specified, previously defined alias. Use 'alias' to
    list the defined aliases.
    """

    def run(self, context, args, kwargs, opargs):
        for name in args:
            if name in context.ml.aliases:
                del context.ml.aliases[name]


@description("Launch shell or shell command")
class ShellCommand(Command):

    """
    Usage: shell <command>

    Examples:
           shell "/usr/local/bin/bash"
           shell "tail /var/log/messages"

    Launch current logged in user's login shell. Type 'exit' to return
    to the CLI. If a command is specified, run the specified command
    then return to the CLI. If the full path to an installed shell is
    specifed, launch the specified shell.
    """

    def __init__(self):
        super(ShellCommand, self).__init__()
        self.closed = False
        self.resize = False

    def run(self, context, args, kwargs, opargs):
        def resize(signo, frame):
            self.resize = True

        def read(data):
            sys.stdout.write(to_ascii(data))
            sys.stdout.flush()

        def close():
            self.closed = True

        self.closed = False
        name = ' '.join(str(i) for i in args) if len(args) > 0 else '/bin/sh'
        if name == '/bin/sh':
            output_msg(context.connection.call_sync(
                'system.general.cowsay',
                "To make configuration changes, return to CLI and use the CLI command set.\n" +
                " Any configuration changes used outside " +
                "of the FreeNAS CLI are not saved to the configuration database.",
                "/usr/local/share/cows/surgery.cow"
            )[0])
        size = get_terminal_size()
        token = context.call_sync('shell.spawn', name, size[1], size[0])
        port = 80
        path = 'dispatcher/shell'
        if urlparse(context.uri).scheme == 'unix':
            port = 5000
            path = 'shell'

        shell = ShellClient(context.hostname, token, port, path)
        shell.on_data(read)
        shell.on_close(close)
        shell.open()

        fd = sys.stdin.fileno()

        if platform.system() != 'Windows':
            signal.signal(signal.SIGWINCH, resize)
            old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)

        while not self.closed:
            if self.resize:
                try:
                    size = get_terminal_size(fd)
                    context.call_sync('shell.resize', token, size[1], size[0])
                except:
                    pass

                self.resize = False

            r, w, x = select.select([fd], [], [], 0.1)
            if fd in r:
                ch = os.read(fd, 1)
                shell.write(ch)

        if platform.system() != 'Windows':
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


@description("Display active IP addresses from all configured network interfaces")
class ShowIpsCommand(Command):

    """
    Usage: showips

    Example: showips

    Display the IP addresses from all configured and active network
    interfaces.
    """

    def run(self, context, args, kwargs, opargs):
        return Sequence(
            _("These are the active ips from all the configured network interfaces"),
            Table(
                [{'ip': x} for x in context.call_sync('network.config.get_my_ips')],
                [Table.Column(_("IP Addresses (ip)"), 'ip')]
            )
        )


@description("Display the URLs for accessing the web GUI")
class ShowUrlsCommand(Command):

    """
    Usage: showurls

    Example: showurls

    Display the URLs for accessing the web GUI.
    """

    def run(self, context, args, kwargs, opargs):
        # Enclose ipv6 urls in '[]' according to ipv6 url spec
        my_ips = [wrap_address(ip) for ip in context.call_sync('network.config.get_my_ips', timeout=60)]
        my_protocols = context.call_sync('system.ui.get_config', timeout=60)
        urls = []
        for proto in my_protocols['webui_protocol']:
            proto_port = my_protocols['webui_{0}_port'.format(proto.lower())]
            if proto_port is not None:
                if proto_port in [80, 443]:
                    for x in my_ips:
                        urls.append({'url': '{0}://{1}'.format(proto.lower(), x)})
                else:
                    for x in my_ips:
                        urls.append({'url': '{0}://{1}:{2}'.format(proto.lower(), x, proto_port)})
        return Table(urls, [Table.Column(_('Web interface URLs'), 'url')])


@description("Login to the CLI as the specified user")
class LoginCommand(Command):

    """
    Usage: login <username> <password>

    Example:
        login my_username secret
        login my_username
        Password: secret
        login
        Username: my_username
        Password: secret


    Login to the CLI as the specified user.
    If password or username is not specified with the command user will be prompted for it.
    """

    def run(self, context, args, kwargs, opargs):
        if not args:
           args = [input('Username:')]
        if len(args) < 2:
            args.append(getpass.getpass('Password:'))
        context.connection.subscribe_events('*')
        context.connection.login_user(args[0], args[1], check_password=True)
        context.user = context.call_sync('session.whoami')
        context.session_id = context.call_sync('session.get_my_session_id')
        context.start_entity_subscribers()
        context.login_plugins()


@description("Exit the CLI")
class ExitCommand(Command):
    """
    Usage: exit

    Example: exit

    Exit the CLI. Note that the CLI will restart if this command
    is run from the local console. The keyboard shortcut for this
    command is (ctrl+d).
    """

    def run(self, context, args, kwargs, opargs):
        try:
            code = int(args[0]) if args else 0
        except ValueError:
            raise CommandException('Exit code must be an integer')

        sys.exit(code)


@description("Display the current CLI user")
class WhoamiCommand(Command):
    """
    Usage: whoami

    Example: whoami

    Display the current CLI user.
    """

    def run(self, context, args, kwargs, opargs):
        return context.user


@description("Display help")
class HelpCommand(Command):

    """
    Usage: help
           help <command>
           help <namespace>
           <namespace> help properties

    Examples:
        help
        help printopt
        help account user create
        account group help properties

    Provide general usage information for current namespace.
    Alternately, provide usage information for specified
    command or for specified namespace.

    To see the available properties for the current or
    specified namespace, use 'help properties'.
    """

    def run(self, context, args, kwargs, opargs):
        ns = self.get_relative_namespace(context)
        arg = args[:]
        obj = context.ml.get_relative_object(ns, args)

        if len(arg) > 0:
            if "/" in arg:
                return textwrap.dedent("""\
                    Usage: /
                    / <namespace>
                    / <namespace> <command>

                    Allows you to navigate or execute commands starting from the root namespace""")
            elif ".." in arg:
                return textwrap.dedent("""\
                    Usage: ..

                    Goes up one level of namespace""")
            elif "-" in arg:
                return textwrap.dedent("""\
                    Usage: -

                    Goes back to the previous namespace""")
            elif "properties" in arg:
                # If the namespace has properties, display a list of the available properties
                if hasattr(obj, 'property_mappings'):
                    prop_dict_list = []
                    for prop in obj.property_mappings:
                        if prop.condition and hasattr(obj, 'entity') and not prop.condition(obj.entity):
                            continue
                        if prop.usage:
                            prop_usage = prop.usage
                        else:
                            if prop.enum:
                                enum_values = prop.enum(obj) if callable(prop.enum) else prop.enum
                                prop_type = "enum " + str(enum_values)
                            else:
                                prop_type = str(prop.type).split('ValueType.')[-1].lower()
                            if not prop.set:
                                prop_usage = "{0}, read_only {1} value".format(prop.descr, prop_type)
                            else:
                                prop_usage = "{0}, accepts {1} values".format(prop.descr, prop_type)
                        prop_dict = {
                            'propname': prop.name,
                            'propusage': ' '.join(prop_usage.split())
                        }
                        prop_dict_list.append(prop_dict)
                if len(prop_dict_list) > 0:
                    return Table(
                        prop_dict_list,
                        [
                            Table.Column('Property', 'propname', ValueType.STRING),
                            Table.Column('Usage', 'propusage', ValueType.STRING),
                        ]
                    )
        if isinstance(obj, Command) or isinstance(obj, FilteringCommand) and obj.__doc__:
            command_name = obj.__class__.__name__
            if (
                hasattr(obj, 'parent') and
                hasattr(obj.parent, 'localdoc') and
                command_name in obj.parent.localdoc.keys()
            ):
                return textwrap.dedent(obj.parent.localdoc[command_name]) + "\n"
            else:
                if inspect.getdoc(obj) is not None:
                    return inspect.getdoc(obj) + "\n"
                else:
                    return _("No help exists for '{0}'.\n".format(arg[0]))

        if isinstance(obj, Namespace):
            # First listing the Current Namespace's commands
            cmd_dict_list = []
            ns_cmds = obj.commands()
            for key, value in ns_cmds.items():
                if hasattr(value, 'description') and value.description is not None:
                    description = value.description
                else:
                    description = obj.get_name()
                value_description = re.sub('<entity>',
                                           str(obj.get_name()),
                                           description)
                cmd_dict = {
                    'cmd': key,
                    'description': value_description,
                }
                cmd_dict_list.append(cmd_dict)

            # Then listing the namespaces available from this namespace
            for nss in obj.namespaces():
                if not isinstance(nss, SingleItemNamespace):
                    if hasattr(nss, 'description') and nss.description is not None:
                        description = nss.description
                    else:
                        description = nss.name
                    namespace_dict = {
                        'cmd': nss.name,
                        'description': description,
                    }
                    cmd_dict_list.append(namespace_dict)

            cmd_dict_list = sorted(cmd_dict_list, key=lambda k: k['cmd'])

            # Finally listing the builtin cmds
            builtin_cmd_dict_list = [
                {"cmd": "/", "description": "Go to the root namespace"},
                {"cmd": "..", "description": "Go up one namespace"},
                {"cmd": "-", "description": "Go back to previous namespace"}
            ]
            filtering_cmd_dict_list = []
            for key, value in context.ml.builtin_commands.items():
                if hasattr(value, 'description') and value.description is not None:
                    description = value.description
                else:
                    description = key
                builtin_cmd_dict = {
                    'cmd': key,
                    'description': description,
                }
                if key in context.ml.pipe_commands:
                    filtering_cmd_dict_list.append(builtin_cmd_dict)
                else:
                    builtin_cmd_dict_list.append(builtin_cmd_dict)

            builtin_cmd_dict_list = sorted(builtin_cmd_dict_list, key=lambda k: k['cmd'])
            filtering_cmd_dict_list = sorted(filtering_cmd_dict_list, key=lambda k: k['cmd'])

            # Finally printing all this out in unix `LESS(1)` pager style
            output_seq = Sequence()
            if cmd_dict_list:
                output_seq.append(
                    Table(cmd_dict_list, [
                        Table.Column('Command', 'cmd', ValueType.STRING),
                        Table.Column('Description', 'description', ValueType.STRING)]))
            # Only display the help on builtin commands if in the RootNamespace
            if obj.__class__.__name__ == 'RootNamespace':
                output_seq.append(
                    Table(builtin_cmd_dict_list, [
                        Table.Column('Global Command', 'cmd', ValueType.STRING),
                        Table.Column('Description', 'description', ValueType.STRING)
                    ]))
                output_seq.append(
                    Table(filtering_cmd_dict_list, [
                        Table.Column('Filter Command', 'cmd', ValueType.STRING),
                        Table.Column('Description', 'description', ValueType.STRING)
                    ]))
            help_message = ""
            if obj.__doc__:
                help_message = inspect.getdoc(obj)
            elif isinstance(obj, SingleItemNamespace):
                help_message = obj.entity_doc()
            if help_message != "":
                output_seq.append("")
                output_seq.append(help_message)
            output_seq.append("")
            return output_seq


@description("List available commands or items in this namespace")
class IndexCommand(Command):
    """
    Usage: ?

    Example:
    ?
    volume ?

    Lists the commands and namespaces accessible from the current
    or specified namespace.
    """

    def run(self, context, args, kwargs, opargs):
        obj = self.get_relative_namespace(context)
        nss = obj.namespaces()
        cmds = obj.commands()

        # Only display builtin items if in the RootNamespace
        outseq = None
        if obj.__class__.__name__ == 'RootNamespace':
            outseq = Sequence(
                _("Global commands:"),
                sorted(['/', '..', '-'] + list(context.ml.base_builtin_commands.keys()))
            )
            outseq += Sequence(_("Filtering commands:"), sorted(list(context.ml.pipe_commands.keys())))

        ns_seq = Sequence(
            _("Current namespace items:"),
            sorted(list(cmds)) +
            [add_tty_formatting(context, quote(ns.get_name())) for ns in sorted(nss, key=lambda i: str(i.get_name()))]
        )
        if outseq is not None:
            outseq += ns_seq
        else:
            outseq = ns_seq

        return outseq


@description("List command variables")
class ListVarsCommand(Command):
    """
    Usage: vars

    Example: vars

    List the command variables for the current scope.
    """

    def run(self, context, args, kwargs, opargs):
        return Table(
            [{'var': k, 'val': v.value} for k, v in self.current_env.items()],
            [Table.Column(_("Variable (var)"), 'var'), Table.Column(_("Value (val)"), 'val')]
        )


@description("Return to the root of the CLI")
class TopCommand(Command):

    """
    Usage: top

    Example: top

    Return to the root of the command tree.
    """

    def run(self, context, args, kwargs, opargs):
        context.ml.path = [context.root_ns]


@description("Clear the screen")
class ClearCommand(Command):

    """
    Usage: clear

    Example: clear

    Clear the screen.
    """

    def run(self, context, args, kwargs, opargs):
        sys.stderr.write('\x1b[2J\x1b[H')


@description("Show the CLI command history")
class HistoryCommand(Command):
    """
    Usage: history <number>

    Example: history
             history 10

    List the commands previously executed in this CLI instance.
    Optionally, provide a number to specify the number of lines,
    from the last line of history, to display.
    """

    def run(self, context, args, kwargs, opargs):
        desired_range = None
        if args:
            if len(args) != 1:
                raise CommandException(_(
                    "Invalid Syntax for history command. For help see 'help <command>'"
                ))
            try:
                desired_range = int(args[0])
            except ValueError:
                raise CommandException(_("Please specify an integer for the history range"))
        histroy_range = readline.get_current_history_length()
        if desired_range is not None and desired_range < histroy_range:
            histroy_range = desired_range + 1
        return Table(
            [{'cmd': readline.get_history_item(i)} for i in range(1, histroy_range)],
            [Table.Column('Command History', 'cmd', ValueType.STRING)]
        )


@description("Run specified script")
class SourceCommand(Command):
    """
    Usage: source </path/filename>
           source </path/filename1> </path/filename2> </path/filename3>

    Example: source /mnt/mypool/myscript

    Run specified file or files, where each file contains a list
    of CLI commands. When creating the source file, separate
    each CLI command with a semicolon or place each
    CLI command on its own line. If multiple files are
    specified, they are run in the order given. If a CLI
    command fails, the source operation aborts.
    """

    def run(self, context, args, kwargs, opargs):
        if len(args) == 0:
            raise CommandException(_("Please provide a filename. For help see 'help <command>'"))
        else:
            for arg in args:
                arg = os.path.expanduser(arg)
                if os.path.isfile(arg):
                    try:
                        with open(arg, 'rb') as f:
                            ast = parse(f.read().decode('utf8'), arg)
                            context.eval_block(ast)
                    except UnicodeDecodeError as e:
                        raise CommandException(_(
                            "Incorrect filetype, cannot parse file: {0}".format(str(e))
                        ))
                else:
                    raise CommandException(_("File {0} does not exist.".format(arg)))


@description("Dump namespace configuration to a series of CLI commands")
class DumpCommand(Command):
    """
    Usage: <namespace> dump
           <namespace> dump <filename>

    Examples:
    update dump
    account user root dump
    dump | less
    dump "/root/mydumpfile.cli"

    Display configuration of specified namespace or, when not specified,
    the current namespace. Optionally, specify the name of the file to
    send the output to.
    """

    def run(self, context, args, kwargs, opargs):
        ns = self.exec_path[-1]
        if len(args) > 1:
            raise CommandException(_('Invalid syntax: {0}. For help see "help <command>"'.format(args)))
        result = []
        if getattr(ns, 'serialize'):
            try:
                for i in ns.serialize():
                    result.append(unparse(i))
            except NotImplementedError:
                return

        contents = '\n'.join(result)
        if len(args) == 1:
            filename = args[0]
            try:
                with open(filename, 'w') as f:
                    f.write(contents)
            except IOError:
                raise CommandException(_('Error writing to file {0}'.format(filename)))
            return _('Configuration successfully dumped to file {0}'.format(filename))
        else:
            return contents


@description("Display the specified message")
class EchoCommand(Command):

    """
    Usage: echo <string_to_display>

    Examples:
    echo "Have a nice Day!"
    output: Have a nice Day!

    echo Hello the current cli session timeout is ${timeout} seconds
    output: Hello the current cli session timeout is 10 seconds

    echo Hi there, you are using the ${language} lang
    output: Hi there, you are using the C lang

    Displays the specified text. If the text contains a symbol, enclose it
    between double quotes. This command can expand and substitute
    variables using the '${variable_name}' syntax, as long as they are not
    enclosed between double quotes.
    """

    def run(sef, context, args, kwargs, opargs):
        if len(args) == 0:
            return ""
        else:
            echo_seq = []
            for i, item in enumerate(args):
                if not (
                    isinstance(item, (Table, output_obj, dict, Sequence, list)) or
                    i == 0 or
                    isinstance(args[i - 1], (Table, output_obj, dict, Sequence, list))
                ):
                    echo_seq[-1] = ' '.join([echo_seq[-1], str(item)])
                elif isinstance(item, list):
                    echo_seq.append(', '.join(item))
                else:
                    echo_seq.append(item)
            return ' '.join(echo_seq)


@description("Display list of pending tasks")
class PendingCommand(Command):
    """
    Usage: pending

    Example: pending

    Display the list of currently pending tasks.
    """

    def run(self, context, args, kwargs, opargs):
        pending = list(filter(
            lambda t: t['session'] == context.session_id,
            context.pending_tasks.values()
        ))

        return Table(pending, [
            Table.Column('Task ID', 'id'),
            Table.Column('Task description', lambda t: translate_task(context, t['name'], t['args'])),
            Table.Column('Task status', describe_task_state)
        ])


@description("Wait for a task to complete and show its progress")
class WaitCommand(Command):
    """
    Usage: wait
           wait <task ID>

    Example: wait
             wait 100

    Show task progress of either all waiting tasks or the
    specified task. Use 'task show' to determine the task ID.
    """

    def run(self, context, args, kwargs, opargs):
        if args:
            try:
                tid = int(args[0])
            except ValueError:
                raise CommandException('Task id argument must be an integer')
        else:
            tid = None
            try:
                tid = context.global_env.find('_last_task_id').value
            except KeyError:
                pass
        if tid is None:
            return 'No recently submitted tasks (which are still active) found'

        return context.wait_for_task_with_progress(tid)


class AttachDebuggerCommand(Command):
    """
    Usage: attach_debugger <path to pydevd egg> <host> <port>
    """

    def run(self, context, args, kwargs, opargs):
        import sys
        sys.path.append(args[0])

        import pydevd
        pydevd.settrace(args[1], port=args[2])


class WCommand(Command):
    """
    Usage: w

    Example: w

    List active CLI sessions.
    """

    def run(self, context, args, kwargs, opargs):
        sessions = context.call_sync('session.get_live_user_sessions')
        return Table(sessions, [
            Table.Column('Session ID', 'id'),
            Table.Column('User name', 'username'),
            Table.Column('Address', 'address'),
            Table.Column('Started at', 'started_at', ValueType.TIME)
        ])


class TimeCommand(Command):
    """
    Usage: time `<code>`

    Measures execution time of <code>
    """

    def run(self, context, args, kwargs, opargs):
        if len(args) < 1 or not isinstance(args[0], Quote):
            raise CommandException("Provide code fragment to evaluate")

        start = datetime.now()
        result = context.eval(args[0].body)
        end = datetime.now()
        msg = "Execution time: {0} seconds".format((end - start).total_seconds())

        return Sequence(*(result + [msg]))


class RemoteCommand(Command):
    """
    Usage: remote `<code>`

    Executes <code> using remote, background CLI instance.
    """

    def run(self, context, args, kwargs, opargs):
        if len(args) < 1 or not isinstance(args[0], Quote):
            raise CommandException("Provide code fragment to evaluate")

        ast = dump_ast(args[0].body)
        tid = context.submit_task('cli.eval.ast', ast)
        return TaskPromise(context, tid)


@description("Scroll through long output")
class MorePipeCommand(PipeCommand):
    """
    Usage: <command> | more
           <command> | less

    Examples: task show | more
              account user show | more
              system advanced show | less

    Allow paging and scrolling through long outputs of text, where
    'more' and 'less' are interchangeable. Press 'q' to return to
    the prompt.
    """

    def __init__(self):
        self.must_be_last = True

    def run(self, context, args, kwargs, opargs, input=None):
        output_less(lambda x: format_output(input, file=x))
        return None


def map_opargs(opargs, context):
    ns = context.pipe_cwd
    mapped_opargs = []
    for k, o, v in opargs:
        if ns.has_property(k):
            mapping = ns.get_mapping(k)
            mapped_opargs.append((mapping.name, o, read_value(v, mapping.type)))
        else:
            raise CommandException(_(
                'Property {0} not found, valid properties are: {1}'.format(
                    k,
                    ','.join([x.name for x in ns.property_mappings if x.list])
                )
            ))
    return mapped_opargs


@description("Filter results based on specified conditions")
class SearchPipeCommand(PipeCommand):
    """
    Usage: <command> | search <key> <op> <value> ...

    Example: account user show | search name==root

    Return an element in a list that matches the given key value.
    """

    def run(self, context, args, kwargs, opargs, input=None):
        return input

    def serialize_filter(self, context, args, kwargs, opargs):
        mapped_opargs = map_opargs(opargs, context)

        if len(kwargs) > 0:
            raise CommandException(_(
                "Invalid syntax {0}. For help see 'help <command>'".format(kwargs)
            ))

        if len(args) > 0:
            raise CommandException(_(
                "Invalid syntax {0}. For help see 'help <command>'".format(args)
            ))

        return {"filter": mapped_opargs}


@description("Finds name of first object matched by the query")
class FindPipeCommand(SearchPipeCommand):
    """
    Usage: <command> | find <key> <op> <value>

    Example: network interface show | find name==vlan0

    Return first item in a list that matches specified conditions.
    """
    def run(self, context, args, kwargs, opargs, input=None):
        ns = context.pipe_cwd
        prop = ns.primary_key
        if isinstance(input, Table):
            try:
                first = next(iter(input.data))
                return prop.do_get(first)
            except (StopIteration, KeyError):
                return None


@description("Select tasks started before or at the specified time")
class OlderThanPipeCommand(PipeCommand):
    """
    Usage: <command> | older_than <hh>:<mm>
           <command> | older_than <hh>:<mm>:<ss>

    Example: task show all | older_than 2:00

    Return all elements of a list that contains time values that are
    older than the given time delta.
    """

    def run(self, context, args, kwargs, opargs, input=None):
        return input

    def serialize_filter(self, context, args, kwargs, opargs):
        return {"filter": [
            ('timestamp', '!=', None),
            ('timestamp', '<=', datetime.now() - parse_timedelta(args[0]))
        ]}


@description("Select tasks started at or since specified time")
class NewerThanPipeCommand(PipeCommand):
    """
    Usage: <command> | newer_than <hh>:<mm>
           <command> | newer_than <hh>:<mm>:<ss>

    Example: task show all | newer_than 2:00

    Return all elements of a list that contains time values that are newer than
    the given time delta.
    """

    def run(self, context, args, kwargs, opargs, input=None):
        return input

    def serialize_filter(self, context, args, kwargs, opargs):
        return {"filter": [
            ('timestamp', '!=', None),
            ('timestamp', '>=', datetime.now() - parse_timedelta(args[0]))
        ]}


@description("Selects last n items")
class TailPipeCommand(PipeCommand):
    """
    Usage: <command> | tail <n>

    Example: log show | tail 10

    Returns last n entries of a list (entity must have the "timestamp" property).
    """
    def run(self, context, args, kwargs, opargs, input=None):
        return input

    def serialize_filter(self, context, args, kwargs, opargs):
        if len(args) == 0:
            raise CommandException(_("Please specify a number to limit."))

        if not isinstance(args[0], int) or len(args) > 1:
            raise CommandException(_(
                "Invalid syntax {0}. For help see 'help <command>'".format(args)
            ))

        return {'params': {
            'sort': ['-timestamp'],
            'limit': args[0],
            'reverse': True
        }}


@description("Exclude results which match specified condition")
class ExcludePipeCommand(PipeCommand):
    """
    Usage: <command> | exclude <key> <op> <value> ...

    Example: account user show | exclude name==root

    Return all the elements of a list that do not match the given key
    value.
    """

    def run(self, context, args, kwargs, opargs, input=None):
        return input

    def serialize_filter(self, context, args, kwargs, opargs):
        mapped_opargs = map_opargs(opargs, context)

        if len(kwargs) > 0:
            raise CommandException(_(
                "Invalid syntax {0}. For help see 'help <command>'".format(kwargs)
            ))

        if len(args) > 0:
            raise CommandException(_(
                "Invalid syntax {0}. For help see 'help <command>'".format(args)
            ))

        result = []
        for i in mapped_opargs:
            result.append(('nor', (i,)))

        return {"filter": result}


@description("Sort results")
class SortPipeCommand(PipeCommand):
    """
    Usage: <command> | sort <field> [<-field> ...]

    Example: account user show | sort name

    Sort the elements of a list by the given key.
    """

    def serialize_filter(self, context, args, kwargs, opargs):
        return {"params": {"sort": args}}

    def run(self, context, args, kwargs, opargs, input=None):
        return input


@description("Limit output to specified number of items")
class LimitPipeCommand(PipeCommand):
    """
    Usage: <command> | limit <n>
           <command> | head <n>

    Example: account user show | limit 10
             network interface show | head 2

    Return only the specified number of elements in a list.
    """

    def serialize_filter(self, context, args, kwargs, opargs):
        if len(args) == 0:
            raise CommandException(_("Please specify a number to limit."))
        if not isinstance(args[0], int) or len(args) > 1:
            raise CommandException(_(
                "Invalid syntax {0}. For help see 'help <command>'".format(args)
            ))
        return {"params": {"limit": args[0]}}

    def run(self, context, args, kwargs, opargs, input=None):
        return input


@description("Display output of the specific field")
class SelectPipeCommand(PipeCommand):
    """
    Usage: <command> | select <field>

    Example: account user show | select name

    Return only the output of the specified field. Use 'help properties' to
    determine the valid field (Property) names for a namespace.
    """

    def run(self, context, args, kwargs, opargs, input=None):
        if len(args) != 1:
            raise CommandException('Please specify exactly one field name')

        if isinstance(input, Table):
            result = Table(None, [Table.Column('Result', 'result')])
            result.data = ({'result': x.get(args[0])} for x in input)
            return result
