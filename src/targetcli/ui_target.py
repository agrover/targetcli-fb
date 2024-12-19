'''
Implements the targetcli target related UI.

This file is part of targetcli.
Copyright (c) 2011-2013 by Datera, Inc

Licensed under the Apache License, Version 2.0 (the "License"); you may
not use this file except in compliance with the License. You may obtain
a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.
'''
from pathlib import Path

try:
    import ethtool
except ImportError:
    ethtool = None
import stat

from configshell_fb import ExecutionError
from rtslib_fb import (
    LUN,
    TPG,
    MappedLUN,
    NetworkPortal,
    NodeACL,
    RTSLibBrokenLink,
    RTSLibError,
    StorageObjectFactory,
    Target,
)

from .ui_backstore import complete_path
from .ui_node import UINode, UIRTSLibNode

auth_params = ('userid', 'password', 'mutual_userid', 'mutual_password')
int_params = ('enable',)
discovery_params = auth_params + int_params
default_portal_listen = "::0"

class UIFabricModule(UIRTSLibNode):
    '''
    A fabric module UI.
    '''
    def __init__(self, fabric_module, parent):
        super().__init__(fabric_module.name,
                                             fabric_module, parent,
                                             late_params=True)
        self.refresh()
        if self.rtsnode.has_feature('discovery_auth'):
            for param in discovery_params:
                if param in int_params:
                    self.define_config_group_param('discovery_auth',
                                                   param, 'number')
                else:
                    self.define_config_group_param('discovery_auth',
                                                   param, 'string')
        self.refresh()

    # Support late params
    #
    # By default the base class will call list_parameters and list_attributes
    # in init. This stops us from being able to lazy-load fabric modules.
    # We declare we support "late_params" to stop this, and then
    # this code overrides the base class methods that involve enumerating
    # this stuff, so we don't need to call list_parameters/attrs (which
    # would cause the module to load) until the ui is actually asking for
    # them from us.
    # Currently fabricmodules don't have these anyways, this is all a CYA thing.
    def list_config_groups(self):
        groups = super().list_config_groups()
        if len(self.rtsnode.list_parameters()):
            groups.append('parameter')
        if len(self.rtsnode.list_attributes()):
            groups.append('attribute')
        return groups

    # Support late params (see above)
    def list_group_params(self, group, writable=None):
        if group not in {"parameter", "attribute"}:
            return super().list_group_params(group,
                                                                 writable)

        params_func = getattr(self.rtsnode, f"list_{group}s")
        params = params_func()
        params_ro = params_func(writable=False)

        ret_list = []
        for param in params:
            p_writable = param not in params_ro
            if writable is not None and p_writable != writable:
                continue
            ret_list.append(param)

        ret_list.sort()
        return ret_list

    # Support late params (see above)
    def get_group_param(self, group, param):
        if group not in {"parameter", "attribute"}:
            return super().get_group_param(group, param)

        if param not in self.list_group_params(group):
            raise ValueError(f"Not such parameter {param} in configuration group {group}")

        description = f"The {param} {group}."
        writable = param in self.list_group_params(group, writable=True)

        return {'name': param, 'group': group, 'type': "string",
                'description': description, 'writable': writable}

    def ui_getgroup_discovery_auth(self, auth_attr):
        '''
        This is the backend method for getting discovery_auth attributes.
        @param auth_attr: The auth attribute to get the value of.
        @type auth_attr: str
        @return: The auth attribute's value
        @rtype: str
        '''
        if auth_attr == 'enable':
            return self.rtsnode.discovery_enable_auth
        return getattr(self.rtsnode, "discovery_" + auth_attr)

    def ui_setgroup_discovery_auth(self, auth_attr, value):
        '''
        This is the backend method for setting discovery auth attributes.
        @param auth_attr: The auth attribute to set the value of.
        @type auth_attr: str
        @param value: The auth's value
        @type value: str
        '''
        self.assert_root()

        if value is None:
            value = ''

        if auth_attr == 'enable':
            self.rtsnode.discovery_enable_auth = value
        else:
            setattr(self.rtsnode, "discovery_" + auth_attr, value)

    def refresh(self):
        self._children = set()
        for target in self.rtsnode.targets:
            self.shell.log.debug(f"Found target {target.wwn} under fabric module {target.fabric_module}.")
            if target.has_feature('tpgts'):
                UIMultiTPGTarget(target, self)
            else:
                UITarget(target, self)

    def summary(self):
        status = None
        msg = []

        fm = self.rtsnode
        if fm.has_feature('discovery_auth') and fm.discovery_enable_auth:
            status = bool(fm.discovery_password and fm.discovery_userid)

            if fm.discovery_authenticate_target:
                msg.append("mutual disc auth")
            else:
                msg.append("1-way disc auth")

        msg.append(f"Targets: {len(self._children)}")

        return (", ".join(msg), status)

    def ui_command_create(self, wwn=None):
        '''
        Creates a new target. The "wwn" format depends on the transport(s)
        supported by the fabric module. If "wwn" is omitted, then a
        target will be created using either a randomly generated WWN of the
        proper type, or the first unused WWN in the list of possible WWNs if
        one is available. If WWNs are constrained to a list (i.e. for hardware
        targets addresses) and all WWNs are in use, the target creation will
        fail. Use the `info` command to get more information abour WWN type
        and possible values.

        SEE ALSO
        ========
        info
        '''
        self.assert_root()

        target = Target(self.rtsnode, wwn, mode='create')
        wwn = target.wwn
        if self.rtsnode.wwns is not None and wwn not in self.rtsnode.wwns:
            self.shell.log.warning("Hardware missing for this WWN")

        if target.has_feature('tpgts'):
            ui_target = UIMultiTPGTarget(target, self)
            self.shell.log.info(f"Created target {wwn}.")
            return ui_target.ui_command_create()

        ui_target = UITarget(target, self)
        self.shell.log.info(f"Created target {wwn}.")
        return self.new_node(ui_target)

    def ui_complete_create(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command create.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'wwn' and self.rtsnode.wwns is not None:
            existing_wwns = [child.wwn for child in self.rtsnode.targets]
            completions = [wwn for wwn in self.rtsnode.wwns
                           if wwn.startswith(text)
                           if wwn not in existing_wwns]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    def ui_command_delete(self, wwn):
        '''
        Recursively deletes the target with the specified wwn, and all
        objects hanging under it.

        SEE ALSO
        ========
        create
        '''
        self.assert_root()
        target = Target(self.rtsnode, wwn, mode='lookup')
        target.delete()
        self.shell.log.info(f"Deleted Target {wwn}.")
        self.refresh()

    def ui_complete_delete(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command delete.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'wwn':
            wwns = [child.name for child in self.children]
            completions = [wwn for wwn in wwns if wwn.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    def ui_command_info(self):
        '''
        Displays information about the fabric module, notably the supported
        transports(s) and accepted wwn format(s), along with supported
        features.
        '''
        fabric = self.rtsnode
        self.shell.log.info(f"Fabric module name: {self.name}")
        self.shell.log.info(f"ConfigFS path: {self.rtsnode.path}")
        self.shell.log.info(f"Allowed WWN types: {', '.join(fabric.wwn_types)}")
        if fabric.wwns is not None:
            self.shell.log.info(f"Allowed WWNs list: {', '.join(fabric.wwns)}")
        self.shell.log.info(f"Fabric module features: {', '.join(fabric.features)}")
        self.shell.log.info(f"Corresponding kernel module: {fabric.kernel_module}")

    def ui_command_version(self):
        '''
        Displays the target fabric module version.
        '''
        version = f"Target fabric module {self.rtsnode.name}: {self.rtsnode.version}"
        self.shell.con.display(version.strip())


class UIMultiTPGTarget(UIRTSLibNode):
    '''
    A generic target UI that has multiple TPGs.
    '''
    def __init__(self, target, parent):
        super().__init__(target.wwn, target, parent)
        self.refresh()

    def refresh(self):
        self._children = set()
        for tpg in self.rtsnode.tpgs:
            UITPG(tpg, self)

    def summary(self):
        try:
            self.rtsnode.fabric_module.to_normalized_wwn(self.rtsnode.wwn)
        except:
            return ("INVALID WWN", False)

        return (f"TPGs: {len(self._children)}", None)

    def ui_command_create(self, tag=None):
        '''
        Creates a new Target Portal Group within the target. The
        tag must be a positive integer value, optionally prefaced
        by 'tpg'. If omitted, the next available Target Portal Group
        Tag (TPGT) will be used.

        SEE ALSO
        ========
        delete
        '''
        self.assert_root()

        if tag:
            if tag.startswith("tpg"):
                tag = tag.removeprefix("tpg")

            try:
                tag = int(tag)
            except ValueError:
                raise ExecutionError("Tag argument must be a number.")

        tpg = TPG(self.rtsnode, tag, mode='create')
        if self.shell.prefs['auto_enable_tpgt']:
            tpg.enable = True

        if tpg.has_feature("auth"):
            tpg.set_attribute("authentication", 0)

        self.shell.log.info(f"Created TPG {tpg.tag}.")

        if tpg.has_feature("nps") and self.shell.prefs['auto_add_default_portal']:
            try:
                NetworkPortal(tpg, f"[{default_portal_listen}]")
                self.shell.log.info("Global pref auto_add_default_portal=true")
                self.shell.log.info("Created default portal listening on all IPs"
                                    f" ({default_portal_listen}), port 3260.")
            except RTSLibError:
                self.shell.log.info("Default portal not created, TPGs within a target cannot share ip:port.")

        ui_tpg = UITPG(tpg, self)
        return self.new_node(ui_tpg)

    def ui_command_delete(self, tag):
        '''
        Deletes the Target Portal Group with TPGT "tag" from the target. The
        tag must be a positive integer matching an existing TPGT.

        SEE ALSO
        ========
        create
        '''
        self.assert_root()
        if tag.startswith("tpg"):
            tag = tag.removeprefix("tpg")
        try:
            tag = int(tag)
        except ValueError:
            raise ExecutionError("Tag argument must be a number.")

        tpg = TPG(self.rtsnode, tag, mode='lookup')
        tpg.delete()
        self.shell.log.info(f"Deleted TPGT {tag}.")
        self.refresh()

    def ui_complete_delete(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command delete.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'tag':
            tags = [child.name[3:] for child in self.children]
            completions = [tag for tag in tags if tag.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions


class UITPG(UIRTSLibNode):
    ui_desc_attributes = {
        'authentication': ('number', 'If set to 1, enforce authentication for this TPG.'),
        'cache_dynamic_acls': ('number', 'If set to 1 in demo mode, cache dynamically generated ACLs.'),
        'default_cmdsn_depth': ('number', 'Default CmdSN (Command Sequence Number) depth.'),
        'default_erl': ('number', 'Default Error Recovery Level.'),
        'demo_mode_discovery': ('number', 'If set to 1 in demo mode, enable discovery.'),
        'demo_mode_write_protect': ('number', 'If set to 1 in demo mode, prevent writes to LUNs.'),
        'fabric_prot_type': ('number', 'Fabric DIF protection type.'),
        'generate_node_acls': ('number', 'If set to 1, allow all initiators to login (i.e. demo mode).'),
        'login_timeout': ('number', 'Login timeout value in seconds.'),
        'netif_timeout': ('number', 'NIC failure timeout in seconds.'),
        'prod_mode_write_protect': ('number', 'If set to 1, prevent writes to LUNs.'),
        't10_pi': ('number', 'If set to 1, enable T10 Protection Information.'),
        'tpg_enabled_sendtargets': ('number', 'If set to 1, the SendTargets discovery response advertises the TPG only if the TPG is enabled.'),
    }

    ui_desc_parameters = {
        'AuthMethod': ('string', 'Authentication method used by the TPG.'),
        'DataDigest': ('string', 'If set to CRC32C, the integrity of the PDU data part is verified.'),
        'DataPDUInOrder': ('yesno', 'If set to Yes, the data PDUs within sequences must be in order.'),
        'DataSequenceInOrder': ('yesno', 'If set to Yes, the data sequences must be in order.'),
        'DefaultTime2Retain': ('number', 'Maximum time, in seconds, after an initial wait, before which an active task reassignment is still possible after an unexpected connection termination or a connection reset.'),
        'DefaultTime2Wait': ('number', 'Minimum time, in seconds, to wait before attempting an explicit/implicit logout or an active task reassignment after an unexpected connection termination or a connection reset.'),
        'ErrorRecoveryLevel': ('number', 'Recovery levels represent a combination of recovery capabilities.'),
        'FirstBurstLength': ('number', 'Maximum amount in bytes of unsolicited data an initiator may send.'),
        'HeaderDigest': ('string', 'If set to CRC32C, the integrity of the PDU header part is verified.'),
        'IFMarker': ('yesno', 'Deprecated according to RFC 7143.'),
        'IFMarkInt': ('string', 'Deprecated according to RFC 7143.'),
        'ImmediateData': ('string', 'Immediate data support.'),
        'InitialR2T': ('yesno', 'If set to No, the default use of R2T (Ready To Transfer) is disabled.'),
        'MaxBurstLength': ('number', 'Maximum SCSI data payload in bytes in a Data-In or a solicited Data-Out iSCSI sequence.'),
        'MaxConnections': ('number', 'Maximum number of connections acceptable.'),
        'MaxOutstandingR2T': ('number', 'Maximum number of outstanding R2Ts per task.'),
        'MaxRecvDataSegmentLength': ('number', 'Maximum data segment length in bytes the target can receive in an iSCSI PDU.'),
        'MaxXmitDataSegmentLength': ('number', 'Outgoing MaxRecvDataSegmentLength sent over the wire during iSCSI login response.'),
        'OFMarker': ('yesno', 'Deprecated according to RFC 7143.'),
        'OFMarkInt': ('string', 'Deprecated according to RFC 7143.'),
        'TargetAlias': ('string', 'Human-readable target name or description.'),
    }

    '''
    A generic TPG UI.
    '''
    def __init__(self, tpg, parent):
        name = "tpg%d" % tpg.tag
        super().__init__(name, tpg, parent)
        self.refresh()

        UILUNs(tpg, self)

        if tpg.has_feature('acls'):
            UINodeACLs(self.rtsnode, self)
        if tpg.has_feature('nps'):
            UIPortals(self.rtsnode, self)

        if self.rtsnode.has_feature('auth') and Path(self.rtsnode.path + "/auth").exists:
            for param in auth_params:
                self.define_config_group_param('auth', param, 'string')

    def summary(self):
        tpg = self.rtsnode
        status = None

        msg = []
        if tpg.has_feature('nexus'):
            msg.append(str(self.rtsnode.nexus))

        if not tpg.enable:
            return ("disabled", False)

        if tpg.has_feature("acls"):
            if "generate_node_acls" in tpg.list_attributes() and \
                    int(tpg.get_attribute("generate_node_acls")):
                msg.append("gen-acls")
            else:
                msg.append("no-gen-acls")

            # 'auth' feature requires 'acls'
            if tpg.has_feature("auth"):
                if not int(tpg.get_attribute("authentication")):
                    msg.append("no-auth")
                    if int(tpg.get_attribute("generate_node_acls")):
                        # if auth=0, g_n_a=1 is recommended
                        status = True
                elif not int(tpg.get_attribute("generate_node_acls")):
                    msg.append("auth per-acl")
                else:
                    msg.append("tpg-auth")

                    status = True
                    if not (tpg.chap_password and tpg.chap_userid):
                        status = False

                    if tpg.authenticate_target:
                        msg.append("mutual auth")
                    else:
                        msg.append("1-way auth")

        return (", ".join(msg), status)

    def ui_getgroup_auth(self, auth_attr):
        return getattr(self.rtsnode, "chap_" + auth_attr)

    def ui_setgroup_auth(self, auth_attr, value):
        self.assert_root()

        if value is None:
            value = ''

        setattr(self.rtsnode, "chap_" + auth_attr, value)

    def ui_command_enable(self):
        '''
        Enables the TPG.

        SEE ALSO
        ========
        disable status
        '''
        self.assert_root()
        if self.rtsnode.enable:
            self.shell.log.info("The TPGT is already enabled.")
        else:
            try:
                self.rtsnode.enable = True
                self.shell.log.info("The TPGT has been enabled.")
            except RTSLibError:
                raise ExecutionError("The TPGT could not be enabled.")

    def ui_command_disable(self):
        '''
        Disables the TPG.

        SEE ALSO
        ========
        enable status
        '''
        self.assert_root()
        if self.rtsnode.enable:
            self.rtsnode.enable = False
            self.shell.log.info("The TPGT has been disabled.")
        else:
            self.shell.log.info("The TPGT is already disabled.")


class UITarget(UITPG):
    '''
    A generic target UI merged with its only TPG.
    '''
    def __init__(self, target, parent):
        super().__init__(TPG(target, 1), parent)
        self._name = target.wwn
        self.target = target
        if self.parent.name != "sbp":
            self.rtsnode.enable = True

    def summary(self):
        try:
            self.target.fabric_module.to_normalized_wwn(self.target.wwn)
        except:
            return ("INVALID WWN", False)

        return super().summary()


class UINodeACLs(UINode):
    '''
    A generic UI for node ACLs.
    '''
    def __init__(self, tpg, parent):
        super().__init__("acls", parent)
        self.tpg = tpg
        self.refresh()

    def refresh(self):
        self._children = set()
        for name in self.all_names():
            UINodeACL(name, self)

    def summary(self):
        return (f"ACLs: {len(self._children)}", None)

    def ui_command_create(self, wwn, add_mapped_luns=None):
        '''
        Creates a Node ACL for the initiator node with the specified wwn.
        The node's wwn must match the expected WWN Type of the target's
        fabric module.

        "add_mapped_luns" can be "true" of "false". If true, then
        after creating the ACL, mapped LUNs will be automatically
        created for all existing LUNs. If the parameter is omitted,
        the global parameter "auto_add_mapped_luns" is used.

        SEE ALSO
        ========
        delete

        '''
        self.assert_root()

        add_mapped_luns = self.ui_eval_param(add_mapped_luns, 'bool',
                                             self.shell.prefs['auto_add_mapped_luns'])

        node_acl = NodeACL(self.tpg, wwn, mode="create")
        ui_node_acl = UINodeACL(node_acl.node_wwn, self)
        self.shell.log.info(f"Created Node ACL for {node_acl.node_wwn}")

        if add_mapped_luns:
            for lun in self.tpg.luns:
                MappedLUN(node_acl, lun.lun, lun.lun, write_protect=False)
                self.shell.log.info("Created mapped LUN %d." % lun.lun)
            self.refresh()

        return self.new_node(ui_node_acl)

    def ui_command_delete(self, wwn):
        '''
        Deletes the Node ACL with the specified wwn.

        SEE ALSO
        ========
        create
        '''
        self.assert_root()
        node_acl = NodeACL(self.tpg, wwn, mode='lookup')
        node_acl.delete()
        self.shell.log.info(f"Deleted Node ACL {wwn}.")
        self.refresh()

    def ui_complete_delete(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command delete.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'wwn':
            wwns = [acl.node_wwn for acl in self.tpg.node_acls]
            completions = [wwn for wwn in wwns if wwn.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    def find_tagged(self, name):
        for na in self.tpg.node_acls:
            if name in {na.node_wwn, na.tag}:
                yield na

    def all_names(self):
        names = set()

        for na in self.tpg.node_acls:
            if na.tag:
                names.add(na.tag)
            else:
                names.add(na.node_wwn)

        return names

    def ui_command_tag(self, wwn_or_tag, new_tag):
        '''
        Tag a NodeACL.

        Usage: tag <wwn_or_tag> <new_tag>

        Tags help manage initiator WWNs. A tag can apply to one or
        more WWNs. This can give a more meaningful name to a single
        initiator's configuration, or allow multiple initiators with
        identical settings to be configured en masse.

        The WWNs described by <wwn_or_tag> will be given the new
        tag. If new_tag already exists, its new members will adopt the
        current tag's configuration.

        Within a tag, the 'info' command shows the WWNs the tag applies to.

        Use 'untag' to remove tags.

        NOTE: tags are only supported in kernel 3.8 and above.
        '''
        if wwn_or_tag == new_tag:
            return

        # Since all WWNs have a '.' in them, let's avoid confusion.
        if '.' in new_tag:
            raise ExecutionError("'.' not permitted in tag names.")

        src = list(self.find_tagged(wwn_or_tag))
        if not src:
            raise ExecutionError(f"wwn_or_tag {wwn_or_tag} not found.")

        old_tag_members = list(self.find_tagged(new_tag))

        # handle overlap
        src_wwns = [na.node_wwn for na in src]
        old_tag_members = [old for old in old_tag_members if old.node_wwn not in src_wwns]

        for na in src:
            na.tag = new_tag

            # if joining a tag, take its config
            if old_tag_members:
                model = old_tag_members[0]

                for mlun in na.mapped_luns:
                    mlun.delete()

                for mlun in model.mapped_luns:
                    MappedLUN(na, mlun.mapped_lun, mlun.tpg_lun, mlun.write_protect)

                if self.parent.rtsnode.has_feature("auth"):
                    for param in auth_params:
                        setattr(na, "chap_" + param, getattr(model, "chap_" + param))

                for item in model.list_attributes(writable=True):
                    na.set_attribute(item, model.get_attribute(item))
                for item in model.list_parameters(writable=True):
                    na.set_parameter(item, model.get_parameter(item))

        self.refresh()

    def ui_command_untag(self, wwn_or_tag):
        '''
        Untag a NodeACL.

        Usage: untag <tag>

        Remove the tag given to one or more initiator WWNs. They will
        return to being displayed by WWN in the configuration tree, and
        will maintain settings from when they were tagged.
        '''
        for na in list(self.find_tagged(wwn_or_tag)):
            na.tag = None

        self.refresh()

    def ui_complete_tag(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command tag
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        completions = [n for n in self.all_names() if n.startswith(text)] if current_param == 'wwn_or_tag' else []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    ui_complete_untag = ui_complete_tag


class UINodeACL(UIRTSLibNode):
    '''
    A generic UI for a node ACL.

    Handles grouping multiple NodeACLs in UI via tags.
    All gets are performed against first NodeACL.
    All sets are performed on all NodeACLs.
    This is to make management of multiple ACLs easier.
    '''
    ui_desc_attributes = {
        'dataout_timeout': ('number', 'Data-Out timeout in seconds before invoking recovery.'),
        'dataout_timeout_retries': ('number', 'Number of Data-Out timeout recovery attempts before failing a path.'),
        'default_erl': ('number', 'Default Error Recovery Level.'),
        'nopin_response_timeout': ('number', 'Nop-In response timeout in seconds.'),
        'nopin_timeout': ('number', 'Nop-In timeout in seconds.'),
        'random_datain_pdu_offsets': ('number', 'If set to 1, request random Data-In PDU offsets.'),
        'random_datain_seq_offsets': ('number', 'If set to 1, request random Data-In sequence offsets.'),
        'random_r2t_offsets': ('number', 'If set to 1, request random R2T (Ready To Transfer) offsets.'),
    }

    ui_desc_parameters = UITPG.ui_desc_parameters

    def __init__(self, name, parent):

        # Don't want to duplicate work in UIRTSLibNode, so call it but
        # del self.rtsnode to make sure we always use self.rtsnodes.
        self.rtsnodes = list(parent.find_tagged(name))
        super().__init__(name, self.rtsnodes[0], parent)
        del self.rtsnode

        if self.parent.parent.rtsnode.has_feature('auth'):
            for parameter in auth_params:
                self.define_config_group_param('auth', parameter, 'string')

        self.refresh()

    def ui_getgroup_auth(self, auth_attr):
        '''
        This is the backend method for getting auths attributes.
        @param auth_attr: The auth attribute to get the value of.
        @type auth_attr: str
        @return: The auth attribute's value
        @rtype: str
        '''
        # All should return same, so just return from the first one
        return getattr(self.rtsnodes[0], "chap_" + auth_attr)

    def ui_setgroup_auth(self, auth_attr, value):
        '''
        This is the backend method for setting auths attributes.
        @param auth_attr: The auth attribute to set the value of.
        @type auth_attr: str
        @param value: The auth's value
        @type value: str
        '''
        self.assert_root()

        if value is None:
            value = ''

        for na in self.rtsnodes:
            setattr(na, "chap_" + auth_attr, value)

    def refresh(self):
        self._children = set()
        for mlun in self.rtsnodes[0].mapped_luns:
            UIMappedLUN(mlun, self)

    def summary(self):
        msg = []

        if self.name != self.rtsnodes[0].node_wwn:
            if len(self.rtsnodes) > 1:
                msg.append(f"(group of {len(self.rtsnodes)})")
            else:
                msg.append(f"({self.rtsnodes[0].node_wwn})")

        status = None
        na = self.rtsnodes[0]
        tpg = self.parent.parent.rtsnode
        if tpg.has_feature("auth") and \
                int(tpg.get_attribute("authentication")):
            if int(tpg.get_attribute("generate_node_acls")):
                msg.append("auth via tpg")
            else:
                status = True
                if not (na.chap_password and na.chap_userid):
                    status = False

                if na.authenticate_target:
                    msg.append("mutual auth")
                else:
                    msg.append("1-way auth")

        msg.append(f"Mapped LUNs: {len(self._children)}")

        return (", ".join(msg), status)

    def ui_command_create(self, mapped_lun, tpg_lun_or_backstore, write_protect=None):
        '''
        Creates a mapping to one of the TPG LUNs for the initiator referenced
        by the ACL. The provided "tpg_lun_or_backstore" will appear to that
        initiator as LUN "mapped_lun". If the "write_protect" flag is set to
        1, the initiator will not have write access to the mapped LUN.

        A storage object may also be given for the "tpg_lun_or_backstore" parameter,
        in which case the TPG LUN will be created for that backstore before
        mapping the LUN to the initiator. If a TPG LUN for the backstore already
        exists, the mapped LUN will map to that TPG LUN.

        Finally, a path to an existing block device or file can be given. If so,
        a storage object of the appropriate type is created with default parameters,
        followed by the TPG LUN and the Mapped LUN.

        SEE ALSO
        ========
        delete
        '''
        self.assert_root()
        try:
            mapped_lun = int(mapped_lun)
        except ValueError:
            raise ExecutionError("mapped_lun must be an integer")

        try:
            if tpg_lun_or_backstore.startswith("lun"):
                tpg_lun_or_backstore = tpg_lun_or_backstore.removeprefix("lun")
            tpg_lun = int(tpg_lun_or_backstore)
        except ValueError:
            try:
                so = self.get_node(tpg_lun_or_backstore).rtsnode
            except ValueError:
                try:
                    so = StorageObjectFactory(tpg_lun_or_backstore)
                    self.shell.log.info(f"Created storage object {so.name}.")
                except RTSLibError:
                    raise ExecutionError("LUN, storage object, or path not valid")
                self.get_node("/backstores").refresh()

            ui_tpg = self.parent.parent

            for lun in ui_tpg.rtsnode.luns:
                if so == lun.storage_object:
                    tpg_lun = lun.lun
                    break
            else:
                lun_object = LUN(ui_tpg.rtsnode, storage_object=so)
                self.shell.log.info(f"Created LUN {lun_object.lun}.")
                ui_lun = UILUN(lun_object, ui_tpg.get_node("luns"))
                tpg_lun = ui_lun.rtsnode.lun

        if tpg_lun in (ml.tpg_lun.lun for ml in self.rtsnodes[0].mapped_luns):
            self.shell.log.warning(
                "Warning: TPG LUN %d already mapped to this NodeACL" % tpg_lun)

        for na in self.rtsnodes:
            mlun = MappedLUN(na, mapped_lun, tpg_lun, write_protect)

        ui_mlun = UIMappedLUN(mlun, self)
        self.shell.log.info(f"Created Mapped LUN {mlun.mapped_lun}.")
        return self.new_node(ui_mlun)

    def ui_complete_create(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command create.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'tpg_lun_or_backstore':
            completions = []
            for backstore in self.get_node('/backstores').children:
                completions = [storage_object.path for storage_object in backstore.children]

            completions.extend(lun.name for lun in self.parent.parent.get_node("luns").children)

            completions.extend(complete_path(text, lambda x: stat.S_ISREG(x) or stat.S_ISBLK(x)))

            completions = [c for c in completions if c.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    def ui_command_delete(self, mapped_lun):
        '''
        Deletes the specified mapped LUN.

        SEE ALSO
        ========
        create
        '''
        self.assert_root()
        for na in self.rtsnodes:
            mlun = MappedLUN(na, mapped_lun)
            mlun.delete()
        self.shell.log.info(f"Deleted Mapped LUN {mapped_lun}.")
        self.refresh()

    def ui_complete_delete(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command delete.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'mapped_lun':
            mluns = [str(mlun.mapped_lun) for mlun in self.rtsnodes[0].mapped_luns]
            completions = [mlun for mlun in mluns if mlun.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    # Override these four methods to handle multiple NodeACLs
    def ui_getgroup_attribute(self, attribute):
        return self.rtsnodes[0].get_attribute(attribute)

    def ui_setgroup_attribute(self, attribute, value):
        self.assert_root()

        for na in self.rtsnodes:
            na.set_attribute(attribute, value)

    def ui_getgroup_parameter(self, parameter):
        return self.rtsnodes[0].get_parameter(parameter)

    def ui_setgroup_parameter(self, parameter, value):
        self.assert_root()

        for na in self.rtsnodes:
            na.set_parameter(parameter, value)

    def ui_command_info(self):
        '''
        Since we don't have a self.rtsnode we can't use the base implementation
        of this method. We also want to not print node_wwn, but list *all*
        wwns for this entry.
        '''
        info = self.rtsnodes[0].dump()
        for item in ('attributes', 'parameters', "node_wwn"):
            if item in info:
                del info[item]
        for name, value in sorted(info.items()):
            if not isinstance(value, (dict, list)):
                self.shell.log.info(f"{name}: {value}")
        self.shell.log.info("wwns:")
        for na in self.parent.find_tagged(self.name):
            self.shell.log.info(na.node_wwn)


class UIMappedLUN(UIRTSLibNode):
    '''
    A generic UI for MappedLUN objects.
    '''
    def __init__(self, mapped_lun, parent):
        name = "mapped_lun%d" % mapped_lun.mapped_lun
        super().__init__(name, mapped_lun, parent)
        self.refresh()

    def summary(self):
        mapped_lun = self.rtsnode
        is_healthy = True
        try:
            tpg_lun = mapped_lun.tpg_lun
        except RTSLibBrokenLink:
            description = "BROKEN LUN LINK"
            is_healthy = False
        else:
            access_mode = 'ro' if mapped_lun.write_protect else 'rw'
            description = "lun%d %s/%s (%s)" \
            % (tpg_lun.lun, tpg_lun.storage_object.plugin,
               tpg_lun.storage_object.name, access_mode)

        return (description, is_healthy)


class UILUNs(UINode):
    '''
    A generic UI for TPG LUNs.
    '''
    def __init__(self, tpg, parent):
        super().__init__("luns", parent)
        self.tpg = tpg
        self.refresh()

    def refresh(self):
        self._children = set()
        for lun in self.tpg.luns:
            UILUN(lun, self)

    def summary(self):
        return (f"LUNs: {len(self._children)}", None)

    def ui_command_create(self, storage_object, lun=None,
                          add_mapped_luns=None):
        '''
        Creates a new LUN in the Target Portal Group, attached to a storage
        object. If the "lun" parameter is omitted, the first available LUN in
        the TPG will be used. If present, it must be a number greater than 0.
        Alternatively, the syntax "lunX" where "X" is a positive number is
        also accepted.

        The "storage_object" may be the path of an existing storage object,
        i.e. "/backstore/pscsi0/mydisk" to reference the "mydisk" storage
        object of the virtual HBA "pscsi0". It also may be the path to an
        existing block device or image file, in which case a storage object
        will be created for it first, with default parameters.

        "add_mapped_luns" can be "true" of "false". If true, then
        after creating the ACL, mapped LUNs will be automatically
        created for all existing LUNs. If the parameter is omitted,
        the global parameter "auto_add_mapped_luns" is used.

        SEE ALSO
        ========
        delete
        '''
        self.assert_root()

        add_mapped_luns = \
                self.ui_eval_param(add_mapped_luns, 'bool',
                                   self.shell.prefs['auto_add_mapped_luns'])

        try:
            so = self.get_node(storage_object).rtsnode
        except ValueError:
            try:
                so = StorageObjectFactory(storage_object)
                self.shell.log.info(f"Created storage object {so.name}.")
            except RTSLibError:
                raise ExecutionError("storage object or path not valid")
            self.get_node("/backstores").refresh()

        if so in (lun.storage_object for lun in self.parent.rtsnode.luns):
            raise ExecutionError(f"lun for storage object {so.plugin}/{so.name} already exists")

        if lun and lun.lower().startswith('lun'):
            lun = lun[3:]
        lun_object = LUN(self.tpg, lun, so)
        self.shell.log.info(f"Created LUN {lun_object.lun}.")
        ui_lun = UILUN(lun_object, self)

        if add_mapped_luns:
            for acl in self.tpg.node_acls:
                mapped_lun = lun or 0
                existing_mluns = [mlun.mapped_lun for mlun in acl.mapped_luns]
                if mapped_lun in existing_mluns:
                    possible_mlun = 0
                    while possible_mlun in existing_mluns:
                        possible_mlun += 1
                    mapped_lun = possible_mlun

                mlun = MappedLUN(acl, mapped_lun, lun_object, write_protect=False)
                self.shell.log.info("Created LUN %d->%d mapping in node ACL %s"
                                    % (mlun.tpg_lun.lun, mlun.mapped_lun, acl.node_wwn))
            self.parent.refresh()

        return self.new_node(ui_lun)

    def ui_complete_create(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command create.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'storage_object':
            storage_objects = []
            for backstore in self.get_node('/backstores').children:
                storage_objects = [storage_object.path for storage_object in backstore.children]
            completions = [so for so in storage_objects if so.startswith(text)]

            completions.extend(complete_path(text, lambda x: stat.S_ISREG(x) or stat.S_ISBLK(x)))
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    def ui_command_delete(self, lun):
        '''
        Deletes the supplied LUN from the Target Portal Group. "lun" must
        be a positive number matching an existing LUN.

        Alternatively, the syntax "lunX" where "X" is a positive number is
        also accepted.

        SEE ALSO
        ========
        create
        '''
        self.assert_root()
        if lun.lower().startswith("lun"):
            lun = lun[3:]
        try:
            lun_object = LUN(self.tpg, lun)
        except:
            raise RTSLibError("Invalid LUN")
        lun_object.delete()
        self.shell.log.info(f"Deleted LUN {lun}.")
        # Refresh the TPG as we need to also refresh acls MappedLUNs
        self.parent.refresh()

    def ui_complete_delete(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command delete.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        if current_param == 'lun':
            luns = [str(lun.lun) for lun in self.tpg.luns]
            completions = [lun for lun in luns if lun.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions


class UILUN(UIRTSLibNode):
    '''
    A generic UI for LUN objects.
    '''
    def __init__(self, lun, parent):
        name = "lun%d" % lun.lun
        super().__init__(name, lun, parent)
        self.refresh()

        self.define_config_group_param("alua", "alua_tg_pt_gp_name", 'string')

    def summary(self):
        lun = self.rtsnode
        is_healthy = True
        try:
            storage_object = lun.storage_object
        except RTSLibBrokenLink:
            description = "BROKEN STORAGE LINK"
            is_healthy = False
        else:
            description = f"{storage_object.plugin}/{storage_object.name}"
            if storage_object.udev_path:
                description += f" ({storage_object.udev_path})"

            description += f" ({lun.alua_tg_pt_gp_name})"

        return (description, is_healthy)

    def ui_getgroup_alua(self, alua_attr):
        return getattr(self.rtsnode, alua_attr)

    def ui_setgroup_alua(self, alua_attr, value):
        self.assert_root()

        if value is None:
            return

        setattr(self.rtsnode, alua_attr, value)

class UIPortals(UINode):
    '''
    A generic UI for TPG network portals.
    '''
    def __init__(self, tpg, parent):
        super().__init__("portals", parent)
        self.tpg = tpg
        self.refresh()

    def refresh(self):
        self._children = set()
        for portal in self.tpg.network_portals:
            UIPortal(portal, self)

    def summary(self):
        return (f"Portals: {len(self._children)}", None)

    def _canonicalize_ip(self, ip_address):
        """
        rtslib expects ipv4 addresses as a dotted-quad string, and IPv6
        addresses surrounded by brackets.
        """

        # Contains a '.'? Must be ipv4, right?
        if "." in ip_address:
            return ip_address
        return "[" + ip_address + "]"

    def ui_command_create(self, ip_address=None, ip_port=None):
        '''
        Creates a Network Portal with the specified IP address and
        port.  If the port is omitted, the default port for
        the target fabric will be used. If the IP address is omitted,
        IN6ADDR_ANY (::0) will be used.

        The default IN6ADDR_ANY (::0) will listen on all IPv6 interfaces
        as well as IPv4, assuming IPV6_V6ONLY sockopt has not been
        set.

        Note: Portals on Link-local IPv6 addresses are currently not
        supported.

        SEE ALSO
        ========
        delete
        '''
        self.assert_root()

        # FIXME: Add a specfile parameter to determine default port
        default_port = 3260
        ip_port = self.ui_eval_param(ip_port, 'number', default_port)
        ip_address = self.ui_eval_param(ip_address, 'string', default_portal_listen)

        if ip_port == default_port:
            self.shell.log.info("Using default IP port %d" % ip_port)
        if ip_address == default_portal_listen:
            self.shell.log.info(f"Binding to INADDR_ANY ({default_portal_listen})")

        portal = NetworkPortal(self.tpg, self._canonicalize_ip(ip_address),
                               ip_port, mode='create')
        self.shell.log.info("Created network portal %s:%d."
                            % (ip_address, ip_port))
        ui_portal = UIPortal(portal, self)
        return self.new_node(ui_portal)

    def ui_complete_create(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command create.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''

        def list_eth_ips():
            if not ethtool:
                return []

            devcfgs = ethtool.get_interfaces_info(ethtool.get_devices())
            addrs = set()
            for d in devcfgs:
                if d.ipv4_address:
                    addrs.add(d.ipv4_address)
                    addrs.add("0.0.0.0")
                for ip6 in d.get_ipv6_addresses():
                    addrs.add(ip6.address)
                    addrs.add("::0")  # only list ::0 if ipv6 present

            return sorted(addrs)

        if current_param == 'ip_address':
            completions = [addr for addr in list_eth_ips()
                           if addr.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions

    def ui_command_delete(self, ip_address, ip_port):
        '''
        Deletes the Network Portal with the specified IP address and port.

        SEE ALSO
        ========
        create
        '''
        self.assert_root()
        portal = NetworkPortal(self.tpg, self._canonicalize_ip(ip_address),
                               ip_port, mode='lookup')
        portal.delete()
        self.shell.log.info(f"Deleted network portal {ip_address}:{ip_port}")
        self.refresh()

    def ui_complete_delete(self, parameters, text, current_param):
        '''
        Parameter auto-completion method for user command delete.
        @param parameters: Parameters on the command line.
        @type parameters: dict
        @param text: Current text of parameter being typed by the user.
        @type text: str
        @param current_param: Name of parameter to complete.
        @type current_param: str
        @return: Possible completions
        @rtype: list of str
        '''
        completions = []
        # TODO: Check if a dict comprehension is acceptable here with supported
        #  XXX: python versions.
        portals = {}
        all_ports = set()
        for portal in self.tpg.network_portals:
            all_ports.add(str(portal.port))
            portal_ip = portal.ip_address.strip('[]')
            if portal_ip not in portals:
                portals[portal_ip] = []
            portals[portal_ip].append(str(portal.port))

        if current_param == 'ip_address':
            completions = [addr for addr in portals if addr.startswith(text)]
            if 'ip_port' in parameters:
                port = parameters['ip_port']
                completions = [addr for addr in completions
                               if port in portals[addr]]
        elif current_param == 'ip_port':
            if 'ip_address' in parameters:
                addr = parameters['ip_address']
                if addr in portals:
                    completions = [port for port in portals[addr]
                                   if port.startswith(text)]
            else:
                completions = [port for port in all_ports
                               if port.startswith(text)]

        if len(completions) == 1:
            return [completions[0] + ' ']
        return completions


class UIPortal(UIRTSLibNode):
    '''
    A generic UI for a network portal.
    '''
    def __init__(self, portal, parent):
        name = f"{portal.ip_address}:{portal.port}"
        super().__init__(name, portal, parent)
        self.refresh()

    def summary(self):
        if self.rtsnode.iser:
            return ('iser', True)
        if self.rtsnode.offload:
            return ('offload', True)
        return ('', True)

    def ui_command_enable_iser(self, boolean):
        '''
        Enables or disables iSER for this NetworkPortal.

        If iSER is not supported by the kernel, this command will do nothing.
        '''

        boolean = self.ui_eval_param(boolean, 'bool', False)
        self.rtsnode.iser = boolean
        self.shell.log.info(f"iSER enable now: {self.rtsnode.iser}")

    def ui_command_enable_offload(self, boolean):
        '''
        Enables or disables offload for this NetworkPortal.

        If offload is not supported by the kernel, this command will do nothing.
        '''

        boolean = self.ui_eval_param(boolean, 'bool', False)
        self.rtsnode.offload = boolean
        self.shell.log.info(f"offload enable now: {self.rtsnode.offload}")
