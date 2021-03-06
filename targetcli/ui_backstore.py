'''
Implements the targetcli backstores related UI.

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

from gi.repository import Gio
import glob
import os
import fcntl
import array
import struct
import re
import stat

from configshell_fb import ExecutionError
from rtslib_fb import BlockStorageObject, FileIOStorageObject
from rtslib_fb import PSCSIStorageObject, RDMCPStorageObject, UserBackedStorageObject
from rtslib_fb import ALUATargetPortGroup
from rtslib_fb import RTSLibError
from rtslib_fb import RTSRoot
from rtslib_fb.utils import get_block_type

from .ui_node import UINode, UIRTSLibNode

default_save_file = "/etc/target/saveconfig.json"

alua_rw_params = ['alua_access_state', 'alua_access_status',
                  'alua_write_metadata', 'alua_access_type', 'preferred',
                  'nonop_delay_msecs', 'trans_delay_msecs',
                  'implicit_trans_secs', 'alua_support_offline',
                  'alua_support_standby', 'alua_support_transitioning',
                  'alua_support_active_nonoptimized',
                  'alua_support_unavailable', 'alua_support_active_optimized']
alua_ro_params = ['tg_pt_gp_id', 'members', 'alua_support_lba_dependent']

alua_state_names = { 0: 'Active/optimized',
                     1: 'Active/non-optimized',
                     2: 'Standby',
                     3: 'Unavailable',
                     4: 'LBA Dependent',
                     14: 'Offline',
                     15: 'Transitioning'}

def human_to_bytes(hsize, kilo=1024):
    '''
    This function converts human-readable amounts of bytes to bytes.
    It understands the following units :
        - B or no unit present for Bytes
        - k, K, kB, KB for kB (kilobytes)
        - m, M, mB, MB for MB (megabytes)
        - g, G, gB, GB for GB (gigabytes)
        - t, T, tB, TB for TB (terabytes)

    Note: The definition of kilo defaults to 1kB = 1024Bytes.
    Strictly speaking, those should not be called "kB" but "kiB".
    You can override that with the optional kilo parameter.

    @param hsize: The human-readable version of the Bytes amount to convert
    @type hsize: string or int
    @param kilo: Optional base for the kilo prefix
    @type kilo: int
    @return: An int representing the human-readable string converted to bytes
    '''
    size = hsize.replace('i', '')
    size = size.lower()
    if not re.match("^[0-9]+[k|m|g|t]?[b]?$", size):
        raise RTSLibError("Cannot interpret size, wrong format: %s" % hsize)

    size = size.rstrip('ib')

    units = ['k', 'm', 'g', 't']
    try:
        power = units.index(size[-1]) + 1
    except ValueError:
        power = 0
        size = int(size)
    else:
        size = int(size[:-1])

    return size * (int(kilo) ** power)

def bytes_to_human(size):
    kilo = 1024.0

    # don't use decimal for bytes
    if size < kilo:
        return "%d bytes" % size
    size /= kilo

    for x in ['KiB', 'MiB', 'GiB', 'TiB', 'PiB']:
        if size < kilo:
            return "%3.1f%s" % (size, x)
        size /= kilo

def complete_path(path, stat_fn):
    filtered = []
    for entry in glob.glob(path + '*'):
        st = os.stat(entry)
        if stat.S_ISDIR(st.st_mode):
            filtered.append(entry + '/')
        elif stat_fn(st.st_mode):
            filtered.append(entry)

    # Put directories at the end
    return sorted(filtered,
                  key=lambda s: '~'+s if s.endswith('/') else s)


class UIALUATargetPortGroup(UIRTSLibNode):
    '''
    A generic UI for ALUATargetPortGroup objects.
    '''
    def __init__(self, alua_tpg, parent):
        name = alua_tpg.name
        super(UIALUATargetPortGroup, self).__init__(name, alua_tpg, parent)
        self.refresh()

        for param in alua_rw_params:
            self.define_config_group_param("alua", param, 'string')

        for param in alua_ro_params:
            self.define_config_group_param("alua", param, 'string', writable=False)

    def ui_getgroup_alua(self, alua_attr):
        return getattr(self.rtsnode, alua_attr)

    def ui_setgroup_alua(self, alua_attr, value):
        self.assert_root()

        if value is None:
            return

        setattr(self.rtsnode, alua_attr, value)

    def summary(self):
        self.rtsnode.alua_access_state
        return ("ALUA state: %s" %
                alua_state_names[self.rtsnode.alua_access_state], True)

class UIALUATargetPortGroups(UINode):
    '''
    ALUA Target Port Group UI
    '''
    def __init__(self, parent):
        super(UIALUATargetPortGroups, self).__init__("alua", parent)
        self.refresh()

    def summary(self):
        return ("ALUA Groups: %d" % len(self.children), None)

    def refresh(self):
        self._children = set([])

        so = self.parent.rtsnode
        for tpg in so.alua_tpgs:
            UIALUATargetPortGroup(tpg, self)

    def ui_command_create(self, name, tag):
        '''
        Create a new ALUA Target Port Group attached to a storage object.
        '''
        self.assert_root()

        so = self.parent.rtsnode
        alua_tpg_object = ALUATargetPortGroup(so, name, int(tag))
        self.shell.log.info("Created ALUA TPG %s." % alua_tpg_object.name)
        ui_alua_tpg = UIALUATargetPortGroup(alua_tpg_object, self)
        return self.new_node(ui_alua_tpg)

    def ui_command_delete(self, name):
        '''
        Delete the ALUA Target Por Group and unmap it from a LUN if needed.
        '''
        self.assert_root()

        so = self.parent.rtsnode
        try:
            alua_tpg_object = ALUATargetPortGroup(so, name)
        except:
            raise RTSLibError("Invalid ALUA group name")

        alua_tpg_object.delete()
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
        if current_param == 'name':
            so = self.parent.rtsnode

            tpgs = [tpg.name for tpg in so.alua_tpgs]
            completions = [tpg for tpg in tpgs if tpg.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        else:
            return completions

class UIBackstores(UINode):
    '''
    The backstores container UI.
    '''
    def __init__(self, parent):
        UINode.__init__(self, 'backstores', parent)
        self.refresh()

    def _user_backstores(self):
        '''
        tcmu-runner (or other daemon providing the same service) exposes a
        DBus ObjectManager-based iface to find handlers it supports.
        '''
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        try:
            mgr_iface = Gio.DBusProxy.new_sync(bus,
                                               Gio.DBusProxyFlags.NONE,
                                               None,
                                               'org.kernel.TCMUService1',
                                               '/org/kernel/TCMUService1',
                                               'org.freedesktop.DBus.ObjectManager',
                                               None)

            for k, v in mgr_iface.GetManagedObjects().items():
                tcmu_iface = Gio.DBusProxy.new_sync(bus,
                                                    Gio.DBusProxyFlags.NONE,
                                                    None,
                                                    'org.kernel.TCMUService1',
                                                    k,
                                                    'org.kernel.TCMUService1',
                                                    None)
                yield (k[k.rfind("/")+1:], tcmu_iface, v)
        except Exception as e:
            return

    def refresh(self):
        self._children = set([])
        UIPSCSIBackstore(self)
        UIRDMCPBackstore(self)
        UIFileIOBackstore(self)
        UIBlockBackstore(self)

        for name, iface, prop_dict in self._user_backstores():
            UIUserBackedBackstore(self, name, iface, prop_dict)

class UIBackstore(UINode):
    '''
    A backstore UI.
    Abstract Base Class, do not instantiate.
    '''
    def __init__(self, plugin, parent):
        UINode.__init__(self, plugin, parent)
        self.refresh()

    def refresh(self):
        self._children = set([])
        for so in RTSRoot().storage_objects:
            if so.plugin == self.name:
                ui_so = self.so_cls(so, self)

    def summary(self):
        return ("Storage Objects: %d" % len(self._children), None)

    def ui_command_delete(self, name, save=None):
        '''
        Recursively deletes the storage object having the specified name. If
        there are LUNs using this storage object, they will be deleted too.

        EXAMPLE
        =======
        delete mystorage
        ----------------
        Deletes the storage object named mystorage, and all associated LUNs.
        '''
        self.assert_root()
        try:
            child = self.get_child(name)
        except ValueError:
            raise ExecutionError("No storage object named %s." % name)

        save = self.ui_eval_param(save, 'bool', False)
        if save:
            rn = self.get_root()
            rn._save_backups(default_save_file)

        child.rtsnode.delete(save=save)
        self.remove_child(child)
        self.shell.log.info("Deleted storage object %s." % name)

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
        if current_param == 'name':
            names = [child.name for child in self.children]
            completions = [name for name in names
                           if name.startswith(text)]
        else:
            completions = []

        if len(completions) == 1:
            return [completions[0] + ' ']
        else:
            return completions

    def setup_model_alias(self, storageobject):
        if self.shell.prefs['export_backstore_name_as_model']:
            try:
                storageobject.set_attribute("emulate_model_alias", 1)
            except RTSLibError:
                raise ExecutionError("'export_backstore_name_as_model' is set but"
                                     " emulate_model_alias\n  not supported by kernel.")


class UIPSCSIBackstore(UIBackstore):
    '''
    PSCSI backstore UI.
    '''
    def __init__(self, parent):
        self.so_cls = UIPSCSIStorageObject
        UIBackstore.__init__(self, 'pscsi', parent)

    def ui_command_create(self, name, dev):
        '''
        Creates a PSCSI storage object, with supplied name and SCSI device. The
        SCSI device "dev" can either be a path name to the device, in which
        case it is recommended to use the /dev/disk/by-id hierarchy to have
        consistent naming should your physical SCSI system be modified, or an
        SCSI device ID in the H:C:T:L format, which is not recommended as SCSI
        IDs may vary in time.
        '''
        self.assert_root()

        if get_block_type(dev) is not None:
            self.shell.log.info("Note: block backstore recommended for "
                                "SCSI block devices")

        so = PSCSIStorageObject(name, dev)
        ui_so = UIPSCSIStorageObject(so, self)
        self.shell.log.info("Created pscsi storage object %s using %s"
                            % (name, dev))
        return self.new_node(ui_so)


class UIRDMCPBackstore(UIBackstore):
    '''
    RDMCP backstore UI.
    '''
    def __init__(self, parent):
        self.so_cls = UIRamdiskStorageObject
        UIBackstore.__init__(self, 'ramdisk', parent)

    def ui_command_create(self, name, size, nullio=None, wwn=None, vendor_id=None):
        '''
        Creates an RDMCP storage object. "size" is the size of the ramdisk.

        SIZE SYNTAX
        ===========
        - If size is an int, it represents a number of bytes.
        - If size is a string, the following units can be used:
            - B or no unit present for bytes
            - k, K, kB, KB for kB (kilobytes)
            - m, M, mB, MB for MB (megabytes)
            - g, G, gB, GB for GB (gigabytes)
            - t, T, tB, TB for TB (terabytes)
        '''
        self.assert_root()

        nullio = self.ui_eval_param(nullio, 'bool', False)
        wwn = self.ui_eval_param(wwn, 'string', None)

        so = RDMCPStorageObject(name, human_to_bytes(size), nullio=nullio, wwn=wwn,
                                vendor_id=vendor_id)
        ui_so = UIRamdiskStorageObject(so, self)
        self.setup_model_alias(so)
        self.shell.log.info("Created ramdisk %s with size %s."
                            % (name, size))
        return self.new_node(ui_so)


class UIFileIOBackstore(UIBackstore):
    '''
    FileIO backstore UI.
    '''
    def __init__(self, parent):
        self.so_cls = UIFileioStorageObject
        UIBackstore.__init__(self, 'fileio', parent)

    def _create_file(self, filename, size, sparse=True):
        try:
            f = open(filename, "w+")
        except (OSError, IOError):
            raise ExecutionError("Could not open %s" % filename)
        try:
            if sparse:
                os.ftruncate(f.fileno(), size)
            else:
                self.shell.log.info("Writing %d bytes" % size)
                try:
                    # Prior to version 3.3, Python does not provide fallocate
                    os.posix_fallocate(f.fileno(), 0, size)
                except AttributeError:
                    while size > 0:
                        write_size = min(size, 1024)
                        f.write("\0" * write_size)
                        size -= write_size
        except (OSError, IOError):
            os.remove(filename)
            raise ExecutionError("Could not expand file to %d bytes" % size)
        except OverflowError:
            raise ExecutionError("The file size is too large (%d bytes)" % size)
        finally:
            f.close()

    def ui_command_create(self, name, file_or_dev, size=None, write_back=None,
                          sparse=None, wwn=None):
        '''
        Creates a FileIO storage object. If "file_or_dev" is a path
        to a regular file to be used as backend, then the "size"
        parameter is mandatory. Else, if "file_or_dev" is a path to a
        block device, the size parameter must be omitted. If
        present, "size" is the size of the file to be used, "file"
        the path to the file or "dev" the path to a block device. The
        "write_back" parameter is a boolean controlling write
        caching. It is enabled by default. The "sparse" parameter is
        only applicable when creating a new backing file. It is a
        boolean stating if the created file should be created as a
        sparse file (the default), or fully initialized.

        SIZE SYNTAX
        ===========
        - If size is an int, it represents a number of bytes.
        - If size is a string, the following units can be used:
            - B or no unit present for bytes
            - k, K, kB, KB for kB (kilobytes)
            - m, M, mB, MB for MB (megabytes)
            - g, G, gB, GB for GB (gigabytes)
            - t, T, tB, TB for TB (terabytes)
        '''
        self.assert_root()

        sparse = self.ui_eval_param(sparse, 'bool', True)
        write_back = self.ui_eval_param(write_back, 'bool', True)
        wwn = self.ui_eval_param(wwn, 'string', None)

        self.shell.log.debug("Using params size=%s write_back=%s sparse=%s"
                             % (size, write_back, sparse))

        file_or_dev = os.path.expanduser(file_or_dev)
        # can't use is_dev_in_use() on files so just check against other
        # storage object paths
        if os.path.exists(file_or_dev):
            for so in RTSRoot().storage_objects:
                if so.udev_path and os.path.samefile(file_or_dev, so.udev_path):
                    raise ExecutionError("storage object for %s already exists: %s" % \
                                             (file_or_dev, so.name))

        if get_block_type(file_or_dev) is not None:
            if size:
                self.shell.log.info("Block device, size parameter ignored")
                size = None
            self.shell.log.info("Note: block backstore preferred for best results")
        else:
            # use given file size only if backing file does not exist
            if os.path.isfile(file_or_dev):
                new_size = os.path.getsize(file_or_dev)
                if size:
                    self.shell.log.info("%s exists, using its size (%s bytes) instead"
                                        % (file_or_dev, new_size))
                size = new_size
            elif os.path.exists(file_or_dev):
                raise ExecutionError("Path %s exists but is not a file" % file_or_dev)
            else:
                # create file and extend to given file size
                if not size:
                    raise ExecutionError("Attempting to create file for new" +
                                         " fileio backstore, need a size")
                size = human_to_bytes(size)
                self._create_file(file_or_dev, size, sparse)

        so = FileIOStorageObject(name, file_or_dev, size,
                                 write_back=write_back, wwn=wwn)
        ui_so = UIFileioStorageObject(so, self)
        self.setup_model_alias(so)
        self.shell.log.info("Created fileio %s with size %s"
                            % (name, so.size))
        return self.new_node(ui_so)

    def ui_complete_create(self, parameters, text, current_param):
        '''
        Auto-completes the file name
        '''
        if current_param != 'file_or_dev':
            return []
        completions = complete_path(text, lambda x: stat.S_ISREG(x) or stat.S_ISBLK(x))
        if len(completions) == 1 and not completions[0].endswith('/'):
            completions = [completions[0] + ' ']
        return completions


class UIBlockBackstore(UIBackstore):
    '''
    Block backstore UI.
    '''
    def __init__(self, parent):
        self.so_cls = UIBlockStorageObject
        UIBackstore.__init__(self, 'block', parent)

    def _ui_block_ro_check(self, dev):
        BLKROGET=0x0000125E
        try:
            f = os.open(dev, os.O_RDONLY)
        except (OSError, IOError):
            raise ExecutionError("Could not open %s" % dev)
        # ioctl returns an int. Provision a buffer for it
        buf = array.array('b', [0] * 4)
        try:
            fcntl.ioctl(f, BLKROGET, buf)
        except (OSError, IOError):
            os.close(f)
            return False

        os.close(f)
        if struct.unpack('I', buf)[0] == 0:
            return False
        return True

    def ui_command_create(self, name, dev, readonly=None, wwn=None,
                          vendor_id=None):
        '''
        Creates an Block Storage object. "dev" is the path to the TYPE_DISK
        block device to use.
        '''
        self.assert_root()

        ro_string = self.ui_eval_param(readonly, 'string', None)
        if ro_string == None:
            # attempt to detect block device readonly state via ioctl
            readonly = self._ui_block_ro_check(dev)
        else:
            readonly = self.ui_eval_param(readonly, 'bool', False)

        wwn = self.ui_eval_param(wwn, 'string', None)

        so = BlockStorageObject(name, dev, readonly=readonly, wwn=wwn,
                                vendor_id=vendor_id)
        ui_so = UIBlockStorageObject(so, self)
        self.setup_model_alias(so)
        self.shell.log.info("Created block storage object %s using %s."
                            % (name, dev))
        return self.new_node(ui_so)

    def ui_complete_create(self, parameters, text, current_param):
        '''
        Auto-completes the device name
        '''
        if current_param != 'dev':
            return []
        completions = complete_path(text, stat.S_ISBLK)
        if len(completions) == 1 and not completions[0].endswith('/'):
            completions = [completions[0] + ' ']
        return completions


class UIUserBackedBackstore(UIBackstore):
    '''
    User backstore UI.
    '''
    def __init__(self, parent, name, iface, prop_dict):
        self.so_cls = UIUserBackedStorageObject
        self.handler = name
        self.iface = iface
        self.prop_dict = prop_dict
        super(UIUserBackedBackstore, self).__init__("user:"+name, parent)

    def refresh(self):
        self._children = set([])
        for so in RTSRoot().storage_objects:
            if so.plugin == 'user' and so.config:
                idx = so.config.find("/")
                handler = so.config[:idx]
                if handler == self.handler:
                    ui_so = self.so_cls(so, self)

    def ui_command_help(self, topic=None):
        super(UIUserBackedBackstore, self).ui_command_help(topic)
        if topic == "create":
            print("CFGSTRING FORMAT")
            print("=================")
            x = self.prop_dict.get("org.kernel.TCMUService1", {})
            print(x.get("ConfigDesc", "No description."))
            print()

    def ui_command_create(self, name, size, cfgstring, wwn=None,
                          hw_max_sectors=None, control=None,
                          vendor_id=None):
        '''
        Creates a User-backed storage object.

        SIZE SYNTAX
        ===========
        - If size is an int, it represents a number of bytes.
        - If size is a string, the following units can be used:
            - B or no unit present for bytes
            - k, K, kB, KB for kB (kilobytes)
            - m, M, mB, MB for MB (megabytes)
            - g, G, gB, GB for GB (gigabytes)
            - t, T, tB, TB for TB (terabytes)
        '''

        size = human_to_bytes(size)
        wwn = self.ui_eval_param(wwn, 'string', None)
        vendor_id = self.ui_eval_param(vendor_id, 'string', None)

        config = self.handler + "/" + cfgstring

        ok, errmsg = self.iface.CheckConfig('(s)', config)
        if not ok:
            raise ExecutionError("cfgstring invalid: %s" % errmsg)

        try:
            so = UserBackedStorageObject(name, size=size, config=config,
                                         wwn=wwn, hw_max_sectors=hw_max_sectors,
                                         control=control, vendor_id=vendor_id)
        except:
            raise ExecutionError("UserBackedStorageObject creation failed.")

        ui_so = UIUserBackedStorageObject(so, self)
        self.shell.log.info("Created user-backed storage object %s size %d."
                            % (name, size))
        return self.new_node(ui_so)

    def ui_command_changemedium(self, name, size, cfgstring):
        size = human_to_bytes(size)
        config = self.handler + "/" + cfgstring

        try:
            rc, errmsg = self.iface.ChangeMedium('(sts)', name, size, config)
        except Exception as e:
            raise ExecutionError("ChangeMedium failed: %s" % e)
        else:
            if rc == 0:
                self.shell.log.info("Medium Changed.")
            else:
                raise ExecutionError("ChangeMedium failed: %s" % errmsg)

class UIStorageObject(UIRTSLibNode):
    '''
    A storage object UI.
    Abstract Base Class, do not instantiate.
    '''
    ui_desc_attributes = {
        'block_size': ('number', 'Block size of the underlying device.'),
        'emulate_3pc': ('number', 'If set to 1, enable Third Party Copy.'),
        'emulate_caw': ('number', 'If set to 1, enable Compare and Write.'),
        'emulate_dpo': ('number', 'If set to 1, turn on Disable Page Out.'),
        'emulate_fua_read': ('number', 'If set to 1, enable Force Unit Access read.'),
        'emulate_fua_write': ('number', 'If set to 1, enable Force Unit Access write.'),
        'emulate_model_alias': ('number', 'If set to 1, use the backend device name for the model alias.'),
        'emulate_rest_reord': ('number', 'If set to 0, the Queue Algorithm Modifier is Restricted Reordering.'),
        'emulate_tas': ('number', 'If set to 1, enable Task Aborted Status.'),
        'emulate_tpu': ('number', 'If set to 1, enable Thin Provisioning Unmap.'),
        'emulate_tpws': ('number', 'If set to 1, enable Thin Provisioning Write Same.'),
        'emulate_ua_intlck_ctrl': ('number', 'If set to 1, enable Unit Attention Interlock.'),
        'emulate_write_cache': ('number', 'If set to 1, turn on Write Cache Enable.'),
        'emulate_pr': ('number', 'If set to 1, enable SCSI Reservations.'),
        'enforce_pr_isids': ('number', 'If set to 1, enforce persistent reservation ISIDs.'),
        'force_pr_aptpl': ('number', 'If set to 1, force SPC-3 PR Activate Persistence across Target Power Loss operation.'),
        'fabric_max_sectors': ('number', 'Maximum number of sectors the fabric can transfer at once.'),
        'hw_block_size': ('number', 'Hardware block size in bytes.'),
        'hw_max_sectors': ('number', 'Maximum number of sectors the hardware can transfer at once.'),
        'control': ('string', 'Comma separated string of control=value tuples that will be passed to kernel control file.'),
        'hw_pi_prot_type': ('number', 'If non-zero, DIF protection is enabled on the underlying hardware.'),
        'hw_queue_depth': ('number', 'Hardware queue depth.'),
        'is_nonrot': ('number', 'If set to 1, the backstore is a non rotational device.'),
        'max_unmap_block_desc_count': ('number', 'Maximum number of block descriptors for UNMAP.'),
        'max_unmap_lba_count': ('number', 'Maximum number of LBA for UNMAP.'),
        'max_write_same_len': ('number', 'Maximum length for WRITE_SAME.'),
        'optimal_sectors': ('number', 'Optimal request size in sectors.'),
        'pi_prot_format': ('number', 'DIF protection format.'),
        'pi_prot_type': ('number', 'DIF protection type.'),
        'queue_depth': ('number', 'Queue depth.'),
        'unmap_granularity': ('number', 'UNMAP granularity.'),
        'unmap_granularity_alignment': ('number', 'UNMAP granularity alignment.'),
        'unmap_zeroes_data': ('number', 'If set to 1, zeroes are read back after an UNMAP.'),
    }

    def __init__(self, storage_object, parent):
        name = storage_object.name
        UIRTSLibNode.__init__(self, name, storage_object, parent)
        self.refresh()

        UIALUATargetPortGroups(self)

    def ui_command_version(self):
        '''
        Displays the version of the current backstore's plugin.
        '''
        self.shell.con.display("Backstore plugin %s %s"
                               % (self.rtsnode.plugin, self.rtsnode.version))

    def ui_command_saveconfig(self, savefile=None):
        '''
        Save configuration of this StorageObject.
        '''
        so = self.rtsnode
        rn = self.get_root()

        if not savefile:
            savefile = default_save_file

        savefile = os.path.expanduser(savefile)

        rn._save_backups(savefile)

        rn.rtsroot.save_to_file(savefile,
                                '/backstores/' + so.plugin  + '/' + so.name)

        self.shell.log.info("Storage Object '%s:%s' config saved to %s."
                            % (so.plugin, so.name, savefile))


class UIPSCSIStorageObject(UIStorageObject):
    def summary(self):
        so = self.rtsnode
        return ("%s %s" % (so.udev_path, so.status), True)


class UIRamdiskStorageObject(UIStorageObject):
    def summary(self):
        so = self.rtsnode

        nullio_str = ""
        if so.nullio:
            nullio_str = "nullio "

        return ("%s(%s) %s" % (nullio_str, bytes_to_human(so.size), so.status), True)


class UIFileioStorageObject(UIStorageObject):
    def summary(self):
        so = self.rtsnode

        if so.write_back:
            wb_str = "write-back"
        else:
            wb_str = "write-thru"

        return ("%s (%s) %s %s" % (so.udev_path, bytes_to_human(so.size),
                                   wb_str, so.status), True)


class UIBlockStorageObject(UIStorageObject):
    def summary(self):
        so = self.rtsnode

        if so.write_back:
            wb_str = "write-back"
        else:
            wb_str = "write-thru"

        ro_str = ""
        if so.readonly:
            ro_str = "ro "

        return ("%s (%s) %s%s %s" % (so.udev_path, bytes_to_human(so.size),
                                   ro_str, wb_str, so.status), True)


class UIUserBackedStorageObject(UIStorageObject):
    def summary(self):
        so = self.rtsnode

        if not so.config:
            config_str = "(no config)"
        else:
            idx = so.config.find("/")
            config_str = so.config[idx+1:]

        return ("%s (%s) %s" % (config_str, bytes_to_human(so.size), so.status), True)
