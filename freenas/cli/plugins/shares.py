#
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
import gettext
from freenas.cli.namespace import (
    EntityNamespace, Command, RpcBasedLoadMixin,
    EntitySubscriberBasedLoadMixin, TaskBasedSaveMixin, description,
    CommandException, ListCommand
)
from freenas.cli.output import ValueType, Table
from freenas.cli.utils import TaskPromise, EntityPromise, post_save, get_item_stub
from freenas.utils import first_or_default
from freenas.utils.query import get


t = gettext.translation('freenas-cli', fallback=True)
_ = t.gettext


@description("Lists users connected to particular share")
class ConnectedUsersCommand(Command):
    def __init__(self, parent):
        self.parent = parent

    def run(self, context, args, kwargs, opargs):
        result = context.call_sync('share.get_connected_clients', self.parent.entity['id'])
        return Table(result, [
            Table.Column(_("IP address"), 'host', ValueType.STRING),
            Table.Column(_("User"), 'user', ValueType.STRING),
            Table.Column(_("Connected at"), 'connected_at', ValueType.TIME)
        ])


class KillConnectionCommand(Command):
    def __init__(self, parent):
        self.parent = parent

    def run(self, context, args, kwargs, opargs):
        tid = context.submit_task('share.terminate_connection', self.parent.type_name, args[0])
        return TaskPromise(context, tid)


class ImportShareCommand(Command):
    """
    Usage: import name path=<path>

    Imports a share from provided path.
    For file share path is representing its parent directory.
    """
    def __init__(self, parent):
        self.parent = parent

    def run(self, context, args, kwargs, opargs):
        try:
            name = args[0]
        except IndexError:
            raise CommandException(_("Please specify the name of share."))
        path = kwargs.get('path', None)
        if not path:
            raise CommandException(_("Please specify a valid path to your share."))

        ns = get_item_stub(context, self.parent, name)

        tid = context.submit_task(
            'share.import',
            path,
            name,
            self.parent.type_name.lower(),
            callback=lambda s, t: post_save(ns, s, t)
        )

        return EntityPromise(context, tid, ns)


@description("Configure and manage shares")
class SharesNamespace(EntitySubscriberBasedLoadMixin, EntityNamespace):
    """
    The share namespace contains the namespaces
    for managing afp, iscsi, nfs, and smb shares
    """

    def __init__(self, name, context):
        super(SharesNamespace, self).__init__(name, context)
        self.context = context
        self.entity_subscriber_name = 'share'
        self.primary_key_name = 'name'

        self.localdoc['ListCommand'] = ("""\
            Usage: show

            Lists shares, optionally doing filtering and sorting.

            Examples:
                show
                show | search name == myshare
                show | search volume == mypool | sort name""")

        self.add_property(
            descr='Share Name',
            name='name',
            get='name',
            set=None,
            createsetable=False,
            usersetable=False
        )

        self.add_property(
            descr='Share Type',
            name='type',
            get='type'
        )

        self.add_property(
            descr='Target',
            name='target',
            get='target_path',
            set=None,
            createsetable=False,
            usersetable=False
        )

        self.add_property(
            descr='Filesystem path',
            name='filesystem_path',
            get='filesystem_path',
            set=None,
            createsetable=False,
            usersetable=False
        )

        self.add_property(
            descr='Description',
            name='description',
            get='description',
            set=None,
            createsetable=False,
            usersetable=False
        )

        self.add_property(
            descr='Enabled',
            name='enabled',
            get='enabled',
            list=True,
            type=ValueType.BOOLEAN
        )

    def commands(self):
        return {
            'show': ListCommand(self),
        }

    def namespaces(self):
        return [
            NFSSharesNamespace('nfs', self.context),
            AFPSharesNamespace('afp', self.context),
            SMBSharesNamespace('smb', self.context),
            WebDAVSharesNamespace('webdav', self.context),
            ISCSISharesNamespace('iscsi', self.context)
        ]


class BaseSharesNamespace(TaskBasedSaveMixin, EntitySubscriberBasedLoadMixin, EntityNamespace):
    def __init__(self, name, type_name, context):
        super(BaseSharesNamespace, self).__init__(name, context)

        self.context = context
        self.type_name = type_name
        self.entity_subscriber_name = 'share'
        self.extra_query_params = [('type', '=', type_name)]
        self.create_task = 'share.create'
        self.update_task = 'share.update'
        self.delete_task = 'share.delete'
        self.required_props = ['name', ['parent', 'dataset', 'path']]
        self.entity_localdoc['DeleteEntityCommand'] = ("""\
            Usage: delete <share name>

            Example: delete myshare

            Deletes a share.""")
        self.localdoc['ListCommand'] = ("""\
            Usage: show

            Lists shares, optionally doing filtering and sorting.

            Examples:
                show
                show | search name == myshare
                show | search volume == mypool | sort name""")

        self.skeleton_entity = {
            'type': type_name,
            'enabled': True,
            'target_type': 'DATASET',
            'properties': {
                '%type': 'Share{0}'.format(type_name.title())
            }
        }

        self.add_property(
            descr='Share name',
            name='name',
            get='name',
            list=True
        )

        self.add_property(
            descr='Share type',
            name='type',
            get='type',
            list=False
        )

        self.add_property(
            descr='Target',
            name='target',
            get=self.get_share_target,
            set=None,
            list=True
        )

        self.add_property(
            descr='Parent',
            name='parent',
            get=None,
            set=self.set_share_parent,
            list=False,
            createsetable=True,
            usersetable=False
        )

        self.add_property(
            descr='Dataset',
            name='dataset',
            get=None,
            set=self.set_share_dataset,
            list=False
        )

        self.add_property(
            descr='Path',
            name='path',
            get=None,
            set=self.set_share_path,
            list=False
        )

        self.add_property(
            descr='Enabled',
            name='enabled',
            get='enabled',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Immutable',
            name='immutable',
            get='immutable',
            list=False,
            usersetable=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Owner',
            name='owner',
            get='permissions.user',
            list=True,
            condition=lambda o: (o['target_type'] in ('DIRECTORY', 'DATASET')) and (o['enabled'])
        )

        self.add_property(
            descr='Group',
            name='group',
            get='permissions.group',
            list=True,
            condition=lambda o: (o['target_type'] in ('DIRECTORY', 'DATASET')) and (o['enabled'])
        )

        self.add_property(
            descr='Enable service',
            name='enable_service',
            get=None,
            set='1',
            list=False,
            type=ValueType.BOOLEAN,
            create_arg=True
        )

        self.add_property(
            descr='Delete associated dataset',
            name='delete_dataset',
            get=None,
            list=False,
            set='0',
            delete_arg=True,
            type=ValueType.BOOLEAN
        )

        self.primary_key = self.get_mapping('name')
        self.primary_key_name = 'name'
        self.save_key_name = 'id'
        self.entity_commands = lambda this: {
            'clients': ConnectedUsersCommand(this)
        }

        self.extra_commands = {
            'import': ImportShareCommand(self),
            'kill': KillConnectionCommand(self)
        }

    def get_share_target(self, obj):
        return '{0} ({1})'.format(obj['target_path'], obj['target_type'].lower())

    def set_share_dataset(self, obj, value):
        obj.update({
            'target_path': value,
            'target_type': 'ZVOL' if type(self) is ISCSISharesNamespace else 'DATASET'
        })

    def set_share_parent(self, obj, value):
        obj.update({
            'target_path': os.path.join(value, obj['name']),
            'target_type': 'ZVOL' if type(self) is ISCSISharesNamespace else 'DATASET'
        })

    def set_share_path(self, obj, value):
        obj.update({
            'target_path': value,
            'target_type': 'FILE' if type(self) is ISCSISharesNamespace else 'DIRECTORY'
        })


@description("NFS shares")
class NFSSharesNamespace(BaseSharesNamespace):
    def __init__(self, name, context):
        super(NFSSharesNamespace, self).__init__(name, 'nfs', context)
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> parent=<volume> <property>=<value> ...
                   create <name> dataset=<volume>/<dataset> <property>=<value> ...
                   create <name> path="/path/to/directory/" <property>=<value> ...

            Examples:
                create myshare parent=mypool
                create myshare parent=mypool read_only=true
                create myshare dataset=mypool/somedataset
                create myshare path="/mnt/mypool/some/directory/"

            Creates an NFS share. For a list of properties, see 'help
            properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set alldirs=true
                      set read_only=true
                      set root_user=myuser
                      set hosts=192.168.1.1, somehost.local

            Sets an NFS share property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='All directories',
            name='alldirs',
            usage=_("""\
            Can be set to yes or no. When set to yes, the NFS client
            can mount any subdirectory within the 'path'."""),
            get='properties.alldirs',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Read only',
            name='read_only',
            usage=_("""\
            Can be set to yes or no. When set to yes, NFS clients are
            prohibited from writing to the share."""),
            get='properties.read_only',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Root user',
            name='root_user',
            usage=_("""\
            If set, the root user is limited to the specified user's
            permissions. This setting prevents 'all_user' from being
            set."""),
            get='properties.maproot_user',
            list=False
        )

        self.add_property(
            descr='Root group',
            name='root_group',
            usage=_("""\
            If set, the root user is limited to the specified group's
            permissions. This setting prevents 'all_group' from being
            set."""),
            get='properties.maproot_group',
            list=False
        )

        self.add_property(
            descr='All user',
            name='all_user',
            usage=_("""\
            If set, the specified user's permissions are used by all
            NFS clients. This setting prevents 'root_user' from being
            set."""),
            get='properties.mapall_user',
            list=False
        )

        self.add_property(
            descr='All group',
            name='all_group',
            usage=_("""\
            If set, the specified group's permissions are used by all
            NFS clients. This setting prevents root_group' from being
            set."""),
            get='properties.mapall_group',
            list=False
        )

        self.add_property(
            descr='Allowed hosts/networks',
            name='hosts',
            usage=_("""\
            A list of allowed IP addresses or hostnames."""),
            get='properties.hosts',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Security',
            name='security',
            usage=_("""\
            Allowed values are sys, krb5 (Kerberos authentication only),
            krb5i (Kerberos authentication and integrity), and krb5p
            (Kerberos authentication and privacy). Requires 'v4' to be
            set in services/nfs."""),
            get='properties.security',
            list=True,
            enum=['sys', 'krb5', 'krb5i', 'krb5p'],
            type=ValueType.SET
        )


@description("AFP shares")
class AFPSharesNamespace(BaseSharesNamespace):
    def __init__(self, name, context):
        super(AFPSharesNamespace, self).__init__(name, 'afp', context)
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> parent=<volume> <property>=<value> ...
                   create <name> dataset=<volume>/<dataset> <property>=<value> ...
                   create <name> path="/path/to/directory/" <property>=<value> ...

            Examples:
                create myshare parent=mypool
                create myshare parent=mypool read_only=true
                create myshare dataset=mypool/somedataset
                create myshare path="/mnt/mypool/some/directory/"

            Creates an AFP share. For a list of properties, see 'help
            properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set time_machine=true
                      set read_only=true
                      set users_allow=myuser, anotheruser
                      set hosts_allow=192.168.1.1, somehost.local

            Sets an AFP share property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='Allowed hosts/networks',
            name='hosts_allow',
            usage=_("""\
            A list of allowed hostnames or IP addresses. Note that setting this
            property will deny any host/IP that is not specified."""),
            get='properties.hosts_allow',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Denied hosts/networks',
            name='hosts_deny',
            usage=_("""\
            A list of denied hostnames or IP addresses. Note that setting this
            property will allow any host/IP that is not specified."""),
            get='properties.hosts_deny',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Allowed users',
            name='users_allow',
            usage=_("""\
            A list of allowed users. Note that setting this property will deny
            any user that is not specified."""),
            get='properties.users_allow',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Allowed groups',
            name='groups_allow',
            usage=_("""\
            A list of allowed groups. Note that setting this property will deny
            any group that is not specified."""),
            get='properties.groups_allow',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Denied users',
            name='users_deny',
            usage=_("""\
            A list ofdenied users. Note that setting this property will allow
            any user that is not specified."""),
            get='properties.users_deny',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Denied groups',
            name='groups_deny',
            usage=_("""\
            A list of denied groups. Note that setting this property will allow
            any group that is not specified."""),
            get='properties.groups_deny',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Read only users',
            name='ro_users',
            get='properties.ro_users',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Read only groups',
            name='ro_groups',
            get='properties.ro_groups',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Read/write users',
            name='rw_users',
            get='properties.rw_users',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Read/write groups',
            name='rw_groups',
            get='properties.rw_groups',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Read only',
            name='read_only',
            get='properties.read_only',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Time machine',
            name='time_machine',
            usage=_("""\
            Can be set to yes or no. When set to yes, FreeNAS will
            advertise itself as a Time Machine disk so it can be
            found by Macs."""),
            get='properties.time_machine',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Zero device numbers',
            name='zero_dev_numbers',
            usage=_("""\
            Can be set to yes or no. When set the device number
            won't be used in the CNID backends"""),
            get='properties.zero_dev_numbers',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='No Stat',
            name='no_stat',
            usage=_("""\
            Can be set to yes or no. When set stat volume path
            when enumerating volumes list is disabled"""),
            get='properties.no_stat',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='AFP3 Privileges',
            name='afp3_privileges',
            usage=_("""\
            Can be set to yes or no. Whether to use AFP3 UNIX privileges"""),
            get='properties.afp3_privileges',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='SMB Compatible',
            name='smb_compatible',
            usage=_("""\
            Can be set to yes or no. Enables SMB compatibility mode"""),
            get='properties.smb_compatible',
            list=False,
            type=ValueType.BOOLEAN
        )


@description("SMB shares")
class SMBSharesNamespace(BaseSharesNamespace):
    def __init__(self, name, context):
        super(SMBSharesNamespace, self).__init__(name, 'smb', context)
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> parent=<volume> <property>=<value> ...
                   create <name> dataset=<volume>/<dataset> <property>=<value> ...
                   create <name> path="/path/to/directory/" <property>=<value> ...

            Examples:
                create myshare parent=mypool
                create myshare parent=mypool read_only=true
                create myshare dataset=mypool/somedataset
                create myshare path="/mnt/mypool/some/directory/"

            Creates a SMB share. For a list of properties, see 'help
            properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set guest_ok=false
                      set read_only=true
                      set browseable=true
                      set hosts_allow=192.168.1.1, somehost.local

            Sets a SMB share property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='Allowed hosts',
            name='hosts_allow',
            usage=_("""\
            A list of allowed hostnames or IP addresses. Note that setting this
            property will deny any host/IP that is not specified."""),
            get='properties.hosts_allow',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Denied hosts',
            name='hosts_deny',
            usage=_("""\
            A list of denied hostnames or IP addresses. Note that setting this
            property will allow any host/IP that is not specified."""),
            get='properties.hosts_deny',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Allowed users',
            name='users_allow',
            usage=_("""\
            A list of allowed users. Note that setting this property will deny
            any user that is not specified."""),
            get='properties.users_allow',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Allowed groups',
            name='groups_allow',
            usage=_("""\
            A list of allowed groups. Note that setting this property will deny
            any group that is not specified."""),
            get='properties.groups_allow',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Denied users',
            name='users_deny',
            usage=_("""\
            A list of denied users. Note that setting this property will allow
            any user that is not specified."""),
            get='properties.users_deny',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Denied groups',
            name='groups_deny',
            usage=_("""\
            A list of denied groups. Note that setting this property will allow
            any group that is not specified."""),
            get='properties.groups_deny',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Read only',
            name='read_only',
            usage=_("""\
            Can be set to yes or no. When set to yes, write access to
            the share is not allowed."""),
            get='properties.read_only',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Guest OK',
            name='guest_ok',
            usage=_("""\
            Can be set to yes or no. When set to yes, no password is
            required to connect to the share and all users share the
            permissions of the guest user set by 'guest_user' in
            service/smb."""),
            get='properties.guest_ok',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Guest only',
            name='guest_only',
            usage=_("""\
            Can be set to yes or no. When set to yes, guest access is
            forced for all connections."""),
            get='properties.guest_only',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Browseable',
            name='browseable',
            usage=_("""\
            Can be set to yes or no. When set to yes, users see the
            contents of other users' home directories. When set to no,
            users see only their own home directory."""),
            get='properties.browseable',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Show hidden files',
            name='show_hidden_files',
            usage=_("""\
            Can be set to yes or no. When set to yes, filenames that
            begin with a dot will be listed."""),
            get='properties.show_hidden_files',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Enable previous versions',
            name='previous_versions',
            usage=_("""\
            Can be set to yes or no. When set to yes, Windows clients will be able to see previous versions
            of the share if there are any snapshots covering share target directory or dataset"""),
            get='properties.previous_versions',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Recyclebin',
            name='recyclebin',
            usage=_("""\
            Can be set to yes or no. When set to yes, file deletion requests result in moving
            deleted file to the .recycle/%U directory."""),
            get='properties.recyclebin',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='VFS objects',
            name='vfs_objects',
            usage=_("""\
            A list of additional vfs objects."""),
            get='properties.vfs_objects',
            list=False,
            type=ValueType.SET
        )

        self.add_property(
            descr='Full audit prefix',
            name='full_audit_prefix',
            usage=_("""\
            Provided string is processed by variables substitution provided by smb.conf(5)
            Example: '%u|%I|%m|%S'."""),
            get='properties.full_audit_prefix',
            list=False,
            type=ValueType.STRING
        )

        self.add_property(
            descr='Full audit priority',
            name='full_audit_priority',
            usage=_("""\
            Priority for the syslog messages.
            Full list of valid values is defned by RFC 3164."""),
            get='properties.full_audit_priority',
            list=False,
            type=ValueType.STRING
        )

        self.add_property(
            descr='Full audit failure',
            name='full_audit_failure',
            usage=_("""\
            Space delimited list, enclosed within double quotes,
            of the VFS operations that should be recorded if they failed."""),
            get='properties.full_audit_failure',
            list=False,
            type=ValueType.STRING
        )

        self.add_property(
            descr='Full audit success',
            name='full_audit_success',
            usage=_("""\
            Space delimited list, enclosed within double quotes,
            of the VFS operations that should be recorded if they succeed."""),
            get='properties.full_audit_success',
            list=False,
            type=ValueType.STRING
        )

        self.add_property(
            descr='Case sensitive',
            name='case_sensitive',
            usage=_("""\
            Case sensitive option controls whether filenames are case sensitive.
            Allowed values yes/no/auto."""),
            get='properties.case_sensitive',
            enum=['AUTO', 'YES', 'NO'],
            list=False,
            type=ValueType.STRING
        )

        self.add_property(
            descr='Allocation roundup size',
            name='allocation_roundup_size',
            usage=_("""\
            Property that allows to tune the allocation size reported to Windows clients.
            Default: 1048576, to disable: 0."""),
            get='properties.allocation_roundup_size',
            list=False,
            type=ValueType.NUMBER
        )

        self.add_property(
            descr='ea support',
            name='ea_support',
            usage=_("""\
            ea support property allow clients to attempt to store OS/2
            style Extended attributes on a share."""),
            get='properties.ea_support',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Store dos attributes',
            name='store_dos_attributes',
            usage=_("""\
            store dos attributes allows SMB to first read the DOS attributes
            before mapping to the UNIX premission bits"""),
            get='properties.store_dos_attributes',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Map archive',
            name='map_archive',
            usage=_("""\
            map archive controls whether DOS style system files should be mapped
            to the UNIX owner execute bit."""),
            get='properties.map_archive',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Map hidden',
            name='map_hidden',
            usage=_("""\
            map hidden controls whether DOS style hidden files should be mapped
            to the UNIX world execute bit."""),
            get='properties.map_hidden',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Map readonly',
            name='map_readonly',
            usage=_("""\
            map readonly controls how the DOS read only attribute should be mapped
            from a UNIX filesystem"""),
            get='properties.map_readonly',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Map system',
            name='map_system',
            usage=_("""\
            map system controls whether DOS style system files should be mapped
            to the UNIX group execute bit."""),
            get='properties.map_system',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Fruit metadata',
            name='fruit_metadata',
            usage=_("""\
            Controls where the MacOS metadata is stored.
            Allowed values: stream | netatalk ."""),
            get='properties.fruit_metadata',
            enum=['STREAM', 'NETATALK'],
            list=False,
            type=ValueType.STRING
        )


@description("WebDAV shares")
class WebDAVSharesNamespace(BaseSharesNamespace):
    def __init__(self, name, context):
        super(WebDAVSharesNamespace, self).__init__(name, 'webdav', context)
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> parent=<volume> <property>=<value> ...
                   create <name> dataset=<volume>/<dataset> <property>=<value> ...
                   create <name> path="/path/to/directory/" <property>=<value> ...

            Examples:
                create myshare parent=mypool
                create myshare parent=mypool read_only=true
                create myshare dataset=mypool/somedataset
                create myshare path="/mnt/mypool/some/directory/"

            Creates WebDAV share. For a list of properties, see 'help
            properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set permission=true
                      set read_only=true

            Sets a WebDAV share property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='Show hidden files',
            name='show_hidden_files',
            usage=_("""\
            Can be set to yes or no. When set to yes, filenames that
            begin with a dot will be listed."""),
            get='properties.show_hidden_files',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Read only',
            name='read_only',
            usage=_("""\
            Can be set to yes or no. When set to yes, users cannot write
            to the share."""),
            get='properties.read_only',
            list=True,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='Webdav user permission',
            name='permission',
            usage=_("""\
            Can be set to yes or no. When set to yes, it automatically sets
            the share's permissions to the webdav user and group."""),
            get='properties.permission',
            list=False,
            type=ValueType.BOOLEAN
        )


@description("iSCSI portals")
class ISCSIPortalsNamespace(RpcBasedLoadMixin, TaskBasedSaveMixin, EntityNamespace):
    def __init__(self, name, context):
        super(ISCSIPortalsNamespace, self).__init__(name, context)
        self.query_call = 'share.iscsi.portal.query'
        self.create_task = 'share.iscsi.portal.create'
        self.update_task = 'share.iscsi.portal.update'
        self.delete_task = 'share.iscsi.portal.delete'
        self.required_props = ['name', 'listen']
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create name=<name> listen=<hostname>:<port>,<hostname>:<port> <property>=<value> ...

            Examples:
                create myiscsi listen=192.168.1.10
                create someiscsi listen="127.0.0.1", "192.168.1.10:8888"

            Creates an iSCSI portal. For a list of properties, see
            'help properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set discovery_auth_group=somegroup
                      set listen="127.0.0.1", "192.168.1.10:8888"

            Sets a iSCSI portal property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='Portal name',
            name='name',
            usage=_("""\
            Mandatory setting. Name of the portal."""),
            get='id'
        )

        self.add_property(
            descr='Discovery auth group',
            name='discovery_auth_group',
            usage=_("""\
            Only set when using CHAP or Mutual CHAP."""),
            get='discovery_auth_group',
            type=ValueType.STRING,
        )

        self.add_property(
            descr='Listen addresses and ports',
            name='listen',
            usage=_("""\
            Mandatory setting. IP address or wildcard of 0.0.0.0.
            To change the default listen port of 3260,
            add a colon and the port number after the IP address.
            When setting multiple address:port values, place each address:port
            pair within double quotes and a comma with space between
            each address."""),
            get=self.get_portals,
            set=self.set_portals,
            type=ValueType.SET
        )

        self.primary_key = self.get_mapping('name')

    def get_portals(self, obj):
        return ['{address}:{port}'.format(**i) for i in obj['listen']]

    def set_portals(self, obj, value):
        def pack(item):
            ret = item.split(':', 2)
            if len(ret) > 1 and not ret[1].isdigit():
                raise CommandException(_("Invalid port number: {0}").format(ret[1]))
            return {
                'address': ret[0],
                'port': int(ret[1]) if len(ret) == 2 else 3260
            }

        obj['listen'] = list(map(pack, value))


@description("iSCSI authentication groups")
class ISCSIAuthGroupsNamespace(RpcBasedLoadMixin, TaskBasedSaveMixin, EntityNamespace):
    def __init__(self, name, context):
        super(ISCSIAuthGroupsNamespace, self).__init__(name, context)
        self.query_call = 'share.iscsi.auth.query'
        self.create_task = 'share.iscsi.auth.create'
        self.update_task = 'share.iscsi.auth.update'
        self.delete_task = 'share.iscsi.auth.delete'
        self.required_props = ['name', 'policy']
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create name=<name> policy=<policy>

            Examples:
                create myiscsi policy=NONE
                create someiscsi policy=DENY

            Creates an iSCSI auth group. For a list of properties, see
            'help properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set name=newname
                      set policy=CHAP

            Sets a iSCSI auth group property. For a list of properties, see
            'help properties'.""")

        self.skeleton_entity = {
            'users': None
        }

        self.add_property(
            descr='Portal name',
            name='name',
            get='id'
        )

        self.add_property(
            descr='Group policy',
            name='policy',
            get='type',
            type=ValueType.STRING,
            enum=['NONE', 'DENY', 'CHAP', 'CHAP_MUTUAL']
        )

        self.primary_key = self.get_mapping('name')
        self.entity_namespaces = lambda this: [
            ISCSIUsersNamespace('users', self.context, this)
        ]


@description("iSCSI auth users")
class ISCSIUsersNamespace(EntityNamespace):
    def __init__(self, name, context, parent):
        super(ISCSIUsersNamespace, self).__init__(name, context)
        self.parent = parent
        self.required_props = ['name', 'secret']
        self.extra_required_props = [['peer_name', 'peer_secret']]
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> secret=<secret>
                   create <name> secret=<secret> peer_name<name> peer_secret=<secret>

            Examples:
                create myiscsi secret=abcdefghijkl
                create myiscsi secret=abcdefghijkl peer_name=peeriscsi peer_secret=mnopqrstuvwx

            Creates an iSCSI auth user. For a list of properties, see
            'help properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set name=newname
                      set secret=yzabcdefghij
                      set peer_name=newpeer
                      set peer_secret=klmnopqrstuv

            Sets a iSCSI auth user property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='User name',
            name='name',
            get='name'
        )

        self.add_property(
            descr='User secret',
            name='secret',
            get='secret'
        )

        self.add_property(
            descr='Peer user name',
            name='peer_name',
            get='peer_name'
        )

        self.add_property(
            descr='Peer secret',
            name='peer_secret',
            get='peer_secret'
        )

        self.primary_key = self.get_mapping('name')

    def get_one(self, name):
        return first_or_default(lambda a: a['name'] == name, self.parent.entity['users'] or [])

    def query(self, params, options):
        return self.parent.entity['users'] or []

    def save(self, this, new=False):
        if new:
            if self.parent.entity['users'] is None:
                 self.parent.entity['users'] = []
            self.parent.entity['users'].append(this.entity)
        else:
            entity = first_or_default(lambda a: a['name'] == this.entity['name'], self.parent.entity['users'])
            entity.update(this.entity)

        return self.parent.save()

    def delete(self, this, kwargs):
        self.parent.entity['users'] = [a for a in self.parent.entity['users'] if a['name'] != this.entity['name']]
        return self.parent.save()


@description("iSCSI targets")
class ISCSITargetsNamespace(RpcBasedLoadMixin, TaskBasedSaveMixin, EntityNamespace):
    def __init__(self, name, context):
        super(ISCSITargetsNamespace, self).__init__(name, context)
        self.query_call = 'share.iscsi.target.query'
        self.create_task = 'share.iscsi.target.create'
        self.update_task = 'share.iscsi.target.update'
        self.delete_task = 'share.iscsi.target.delete'
        self.required_props = ['name']
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> <property>=<value> ...

            Examples:
                create myiscsi
                create myiscsi description="some share" auth_group=somegroup

            Creates an iSCSI target. For a list of properties, see
            'help properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set name=newname
                      set description="describe me"
                      set auth_group=group

            Sets a iSCSI target property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='Target name',
            name='name',
            get='id'
        )

        self.add_property(
            descr='Target description',
            name='description',
            get='description'
        )

        self.add_property(
            descr='Auth group',
            name='auth_group',
            get='auth_group'
        )

        self.add_property(
            descr='Portal group',
            name='portal_group',
            get='portal_group'
        )

        self.primary_key = self.get_mapping('name')
        self.entity_namespaces = lambda this: [
            ISCSITargetMapingNamespace('luns', self.context, this)
        ]


@description("iSCSI luns")
class ISCSITargetMapingNamespace(EntityNamespace):
    def __init__(self, name, context, parent):
        super(ISCSITargetMapingNamespace, self).__init__(name, context)
        self.parent = parent
        self.required_props = ['number', 'name']
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <number> <name>=<name>

            Examples:
                create 12 name=myiscsi

            Creates an iSCSI lun. For a list of properties, see
            'help properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set name=newname
                      set number=13

            Sets a iSCSI lun property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='LUN number',
            name='number',
            get='number',
            type=ValueType.NUMBER
        )

        self.add_property(
            descr='Share name',
            name='name',
            get='name'
        )

        self.primary_key = self.get_mapping('number')

    def get_one(self, name):
        return first_or_default(lambda a: a['number'] == name, self.parent.entity['extents'])

    def query(self, params, options):
        return self.parent.entity.get('extents', [])

    def save(self, this, new=False):
        if new:
            self.parent.entity['extents'].append(this.entity)
        else:
            entity = first_or_default(lambda a: a['number'] == this.entity['number'], self.parent.entity['extents'])
            entity.update(this.entity)

        return self.parent.save()

    def delete(self, this, kwargs):
        self.parent.entity['extents'] = [a for a in self.parent.entity['extents'] if a['number'] != this.entity['number']]
        return self.parent.save()


@description("iSCSI shares")
class ISCSISharesNamespace(BaseSharesNamespace):
    def __init__(self, name, context):
        super(ISCSISharesNamespace, self).__init__(name, 'iscsi', context)
        self.required_props.append('size')
        self.localdoc['CreateEntityCommand'] = ("""\
            Usage: create <name> parent=<volume> size=<size> <property>=<value> ...
                   create <name> dataset=<volume>/<dataset> size=<size> <property>=<value> ...
                   create <name> path="/path/to/directory/" size=<size> <property>=<value> ...


            Examples:
                create myiscsi parent=mypool size=3G
                create myiscsi dataset=mypool/somedataset size=3G
                create myiscsi path="/mnt/mypool/some/directory/" size=3G

            Creates an iSCSI share. For a list of properties, see
            'help properties'.""")
        self.entity_localdoc['SetEntityCommand'] = ("""\
            Usage: set <property>=<value> ...

            Examples: set name=newname
                      set block_size=256
                      set compression=gzip

            Sets a iSCSI share property. For a list of properties, see
            'help properties'.""")

        self.add_property(
            descr='Serial number',
            name='serial',
            get='properties.serial',
            list=True
        )

        self.add_property(
            descr='Size',
            name='size',
            get='properties.size',
            usersetable=False,
            list=True,
            type=ValueType.SIZE
        )

        self.add_property(
            descr='Block size',
            name='block_size',
            get='properties.block_size',
            type=ValueType.NUMBER
        )

        self.add_property(
            descr='Physical block size reporting',
            name='physical_block_size',
            get='properties.physical_block_size',
            list=False,
            type=ValueType.BOOLEAN
        )

        self.add_property(
            descr='RPM',
            name='rpm',
            get='properties.rpm',
            list=False,
            enum=['UNKNOWN', 'SSD', '5400', '7200', '10000', '15000']
        )

    def namespaces(self):
        return list(super(ISCSISharesNamespace, self).namespaces()) + [
            ISCSIPortalsNamespace('portals', self.context),
            ISCSITargetsNamespace('targets', self.context),
            ISCSIAuthGroupsNamespace('auth', self.context)
        ]


def find_share_namespace(context, task):
    if task['name'] == 'share.create':
        share_type = get(task, 'args.0.type')

    elif task['name'] == 'share.update':
        share_id = get(task, 'args.0')
        share_type = context.entity_subscribers['share'].query(('id', '=', share_id), single=True)

    else:
        return

    if share_type == 'smb':
        return SMBSharesNamespace

    if share_type == 'nfs':
        return NFSSharesNamespace

    if share_type == 'afp':
        return AFPSharesNamespace

    if share_type == 'webdav':
        return WebDAVSharesNamespace

    if share_type == 'iscsi':
        return ISCSISharesNamespace


def _init(context):
    context.attach_namespace('/', SharesNamespace('share', context))
    context.map_tasks('share.*', find_share_namespace)
    context.map_tasks('share.iscsi.target.*', ISCSITargetsNamespace)
    context.map_tasks('share.iscsi.auth.*', ISCSIAuthGroupsNamespace)
