'''
Implements the targetcli root UI.

This file is part of targetcli.
Copyright (c) 2011 by RisingTide Systems LLC

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, version 3 (AGPLv3).

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

from rtslib import RTSRoot
from configshell import ExecutionError
from ui_node import UINode
from socket import gethostname
from ui_target import UIFabricModule
from ui_backstore import UIBackstores
import json
import shutil
import os
import stat

default_save_file = "/etc/target/saveconfig.json"

class UIRoot(UINode):
    '''
    The targetcli hierarchy root node.
    '''
    def __init__(self, shell, as_root=False):
        UINode.__init__(self, '/', shell=shell)
        self.as_root = as_root

    def refresh(self):
        '''
        Refreshes the tree of target fabric modules.
        '''
        self._children = set([])

        UIBackstores(self)

        for fabric_module in RTSRoot().fabric_modules:
            UIFabricModule(fabric_module, self)

    def ui_command_saveconfig(self, savefile=default_save_file):
        '''
        Saves the current configuration to a file so that it can be restored
        on next boot.
        '''
        self.assert_root()

        savefile = os.path.expanduser(savefile)

        backupfile = savefile + ".backup"
        try:
            shutil.move(savefile, backupfile)
            self.shell.log.info("Existing file %s backed up to %s" % \
                                    (savefile, backupfile.split('/')[-1]))
        except IOError:
            pass

        with open(savefile, "w+") as f:
            os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write(json.dumps(RTSRoot().dump(), sort_keys=True, indent=2))
            f.write("\n")

        self.shell.log.info("Configuration saved to %s" % savefile)

    def ui_command_restoreconfig(self, savefile=default_save_file, clear_existing=False):
        '''
        Restores configuration from a file.
        '''
        self.assert_root()

        savefile = os.path.expanduser(savefile)

        if not os.path.isfile(savefile):
            self.shell.log.info("Restore file %s not found" % savefile)
            return

        with open(savefile, "r") as f:
            try:
                errors = RTSRoot().restore(json.loads(f.read()), clear_existing)
            except ValueError:
                self.shell.log.error("Error parsing savefile: %s" % savefile)
                return

        if errors:
            self.shell.log.error("Configuration restored, %s recoverable errors" % errors)
        else:
            self.shell.log.info("Configuration restored from %s" % savefile)

        self.refresh()

    def ui_command_clearconfig(self, confirm=None):
        '''
        Removes entire configuration of backstores and targets
        '''
        self.assert_root()

        confirm = self.ui_eval_param(confirm, 'bool', False)

        RTSRoot().clear_existing(confirm=confirm)

        self.shell.log.info("All configuration cleared")

        self.refresh()

    def ui_command_version(self):
        '''
        Displays the targetcli and support libraries versions.
        '''
        from targetcli import __version__ as targetcli_version
        self.shell.log.info("targetcli version %s" % targetcli_version)

    def ui_command_sessions(self, action="list", sid=None):
        '''
        Displays a detailed list of all open sessions.

        PARAMETERS
        ==========

        I{action}
        ---------
        The I{action} is one of:
            - B{list} gives a short session list
            - B{details} gives a detailed list

        I{sid}
        ------
        You can specify an I{sid} to only list this one,
        with or without details.

        SEE ALSO
        ========
        status
        '''

        indent_step = 4
        base_steps = 0
        action_list = ["list", "details"]
        found = False

        def indent_print(text, steps):
            console = self.shell.con
            console.display(console.indent(text, indent_step * steps),
                            no_lf=True)

        if action not in action_list:
            raise ExecutionError("action must be one of: %s" %
                                                    ", ".join(action_list))
        if sid is not None:
            try:
                int(sid)
            except ValueError, e:
                raise ExecutionError("sid must be a number, '%s' given" % sid)

        for session in RTSRoot().sessions:

            if sid is None or int(sid) == session.id:
                found = True
                acl = session.parent_nodeacl
                indent_print("alias: %s\tsid: %i  type: %s  state: %s" % (
                                                session.alias, session.id,
                                                session.type, session.state),
                             base_steps)

                if action == "details":
                    if self.as_root:
                        if acl.authenticate_target:
                            auth = "authenticated"
                        else:
                            auth = "NOT AUTHENTICATED"
                        indent_print("%s (%s)" % (acl.node_wwn, auth),
                                     base_steps + 1)
                    else:
                        indent_print("%s" % acl.node_wwn, base_steps + 1)

                    for mlun in acl.mapped_luns:
                        number = str(mlun.mapped_lun)
                        dev = mlun.tpg_lun.storage_object.udev_path
                        if mlun.write_protect:
                            mode = " (r)"
                        else:
                            mode = " (rw)"
                        indent_print(number + " " + dev + mode, base_steps + 1)

                    for connection in session.connections:
                        indent_print("address: %s (%s)  cid: %i  state: %s" % (
                                            connection.address,
                                            connection.transport,
                                            connection.cid, connection.cstate),
                                     base_steps + 1)
        if not found:
            if sid is None:
                indent_print("(no open sessions)", base_steps)
            else:
                raise ExecutionError("no session found with sid %i" % int(sid))

