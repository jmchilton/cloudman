"""Galaxy CM master manager"""
import commands
import fileinput
import logging
import logging.config
import os
import subprocess
import threading
import time
import datetime as dt
import json
import shutil


from cm.services import ServiceRole
from cm.services import ServiceType
from cm.services import service_states
from cm.services.apps.galaxy import GalaxyService
from cm.services.apps.galaxyreports import GalaxyReportsService
from cm.services.apps.hadoop import HadoopService
from cm.services.apps.htcondor import HTCondorService
from cm.services.apps.migration import MigrationService
from cm.services.apps.postgres import PostgresService
from cm.services.apps.proftpd import ProFTPdService
from cm.services.apps.pss import PSSService
from cm.services.apps.sge import SGEService
from cm.services.autoscale import Autoscale
from cm.services.data.filesystem import Filesystem
from cm.util import (cluster_status, comm, instance_lifecycle, instance_states,
        misc, spot_states, Time)
from cm.util.decorators import TestFlag
from cm.util.manager import BaseConsoleManager

import cm.util.paths as paths
from boto.exception import EC2ResponseError, S3ResponseError

log = logging.getLogger('cloudman')

# Time well in past to seend reboot, last comm times with.
TIME_IN_PAST = dt.datetime(2012, 1, 1, 0, 0, 0)

s3_rlock = threading.RLock()


def synchronized(rlock):
    """
        Synchronization decorator
        http://stackoverflow.com/a/490090
    """
    def wrap(f):
        def newFunction(*args, **kw):
            with rlock:
                return f(*args, **kw)
        return newFunction
    return wrap


class ConsoleManager(BaseConsoleManager):
    node_type = "master"

    def __init__(self, app):
        self.startup_time = Time.now()
        log.debug("Initializing console manager - cluster start time: %s" %
                  self.startup_time)
        self.app = app
        self.console_monitor = ConsoleMonitor(self.app)
        self.root_pub_key = None
        self.cluster_status = cluster_status.STARTING
        self.num_workers_requested = 0  # Number of worker nodes requested by user
        # The actual worker nodes (note: this is a list of Instance objects)
        # (because get_worker_instances currently depends on tags, which is only
        # supported by EC2, get the list of instances only for the case of EC2 cloud.
        # This initialization is applicable only when restarting a cluster.
        self.worker_instances = self.get_worker_instances() if (self.app.cloud_type == 'ec2' or self.app.cloud_type == 'openstack') else []
        self.disk_total = "0"
        self.disk_used = "0"
        self.disk_pct = "0%"
        self.manager_started = False
        self.cluster_manipulation_in_progress = False
        # If this is set to False, the master instance will not be an execution
        # host in SGE and thus not be running any jobs
        self.master_exec_host = True
        self.initial_cluster_type = None
        self.services = []
        # Static data - get snapshot IDs from the default bucket and add respective file systems
        self.snaps = self._load_snapshot_data()
        self.default_galaxy_data_size = 0

    def add_master_service(self, new_service):
        if not self.get_services(svc_name=new_service.name):
            log.debug("Adding service %s into the master service registry" % new_service.name)
            self.services.append(new_service)
            self._update_dependencies(new_service, "ADD")
        else:
            log.debug("Would add master service %s but one already exists" % new_service.name)

    def remove_master_service(self, service_to_remove):
        self.services.remove(service_to_remove)
        self._update_dependencies(service_to_remove, "REMOVE")

    def _update_dependencies(self, new_service, action):
        """
        Updates service dependencies when a new service is added.
        Iterates through all services and if action is "ADD",
        and the newly added service fulfills the requirements of an
        existing service, sets the new service as the service assigned
        to fulfill the existing service.
        If the action is "REMOVE", then the dependent service's
        assigned service property is set to null for all services
        which depend on the new service.
        """
        log.debug("Updating dependencies for service {0}".format(new_service.name))
        for svc in self.services:
            if action == "ADD":
                for req in new_service.dependencies:
                    if req.is_satisfied_by(svc):
                        # log.debug("Service {0} has a dependency on role {1}. Dependency updated during service action: {2}".format(
                        #     req.owning_service.name, new_service.name, action))
                        req.assigned_service = svc
            elif action == "REMOVE":
                for req in svc.dependencies:
                    if req.is_satisfied_by(new_service):
                        # log.debug("Service {0} has a dependency on role {1}. Dependency updated during service action: {2}".format(
                        #     req.owning_service.name, new_service.name, action))
                        req.assigned_service = None

    def _stop_app_level_services(self):
        """ Convenience function that suspends SGE jobs and removes Galaxy &
        Postgres services, thus allowing system level operations to be performed."""
        # Suspend all SGE jobs
        log.debug("Suspending SGE queue all.q")
        misc.run('export SGE_ROOT=%s; . $SGE_ROOT/default/common/settings.sh; %s/bin/lx24-amd64/qmod -sq all.q'
                 % (self.app.path_resolver.sge_root, self.app.path_resolver.sge_root), "Error suspending SGE jobs", "Successfully suspended all SGE jobs.")
        # Stop application-level services managed via CloudMan
        # If additional service are to be added as things CloudMan can handle,
        # the should be added to do for-loop list (in order in which they are
        # to be removed)
        if self.initial_cluster_type == 'Galaxy':
            for svc_role in [ServiceRole.GALAXY, ServiceRole.GALAXY_POSTGRES,
                             ServiceRole.PROFTPD]:
                try:
                    svc = self.get_services(svc_role=svc_role)
                    if svc:
                        svc[0].remove()
                except IndexError, e:
                    log.error("Tried removing app level service '%s' but failed: %s"
                              % (svc_role, e))

    def _start_app_level_services(self):
        # Resume application-level services managed via CloudMan
        # If additional service are to be added as things CloudMan can handle,
        # the should be added to do for-loop list (in order in which they are
        # to be added)
        for svc_role in [ServiceRole.GALAXY_POSTGRES, ServiceRole.PROFTPD,
                         ServiceRole.GALAXY]:
            try:
                svc = self.get_services(svc_role=svc_role)
                if svc:
                    svc[0].add()
            except IndexError, e:
                log.error("Tried adding app level service '%s' but failed: %s"
                          % (ServiceRole.to_string([svc_role]), e))
        log.debug("Unsuspending SGE queue all.q")
        misc.run('export SGE_ROOT=%s; . $SGE_ROOT/default/common/settings.sh; %s/bin/lx24-amd64/qmod -usq all.q'
                 % (self.app.path_resolver.sge_root, self.app.path_resolver.sge_root),
                 "Error unsuspending SGE jobs",
                 "Successfully unsuspended all SGE jobs")

    def recover_monitor(self, force='False'):
        if self.console_monitor:
            if force == 'True':
                self.console_monitor.shutdown()
            else:
                return False
        self.console_monitor = ConsoleMonitor(self.app)
        self.console_monitor.start()
        return True

    def snapshot_status(self):
        """
        Get the status of a file system volume currently being snapshoted. This
        method looks through all the file systems and all volumes assoc. with a
        file system and returns the status and progress for thee first volume
        going through the snapshot process.
        In addition, if a file system is marked as needing to 'grow' or sharing
        the cluster is currently pending but no volumes are currently being
        snapshoted, the method returns 'configuring' as the status.

        :rtype: array of strings of length 2
        :return: A pair of values as strings indicating (1) the status (e.g.,
        pending, complete) of the snapshot and (2) the progress.
        """
        fsarr = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        for fs in fsarr:
            for vol in fs.volumes:
                if vol.snapshot_status is not None:
                    return (vol.snapshot_status, vol.snapshot_progress)
            # No volume is being snapshoted; check if waiting to 'grow' one
            if fs.grow:
                return ("configuring", None)
        if self.cluster_manipulation_in_progress:
            return ("configuring", None)
        return (None, None)

    @TestFlag([])
    @synchronized(s3_rlock)
    def _load_snapshot_data(self):
        """
        Retrieve and return information about the default filesystems.
        This is done by retrieving ``snaps.yaml`` from the default bucket and
        parsing it to match the current cloud, region, and deployment.
        Returns a list of dictionaries.
        """
        s3_conn = self.app.cloud_interface.get_s3_connection()
        snaps_file = 'cm_snaps.yaml'
        snaps = []
        cloud_name = self.app.ud.get('cloud_name', 'amazon').lower()
        # Get a list of default file system data sources
        if s3_conn and misc.get_file_from_bucket(s3_conn, self.app.ud['bucket_default'],
           'snaps.yaml', snaps_file):
            pass
        elif misc.get_file_from_public_bucket(self.app.ud, self.app.ud['bucket_default'], 'snaps.yaml', snaps_file):
            log.warn("Couldn't get snaps.yaml from bucket: %s. However, managed to retrieve from public s3 url instead." % self.app.ud['bucket_default'])
        else:
            log.error("Couldn't get snaps.yaml at all! Will not be able to create Galaxy Data and Index volumes.")
            return []

        snaps_file = misc.load_yaml_file(snaps_file)
        if 'static_filesystems' in snaps_file:
            # Old snaps.yaml format
            snaps = snaps_file['static_filesystems']
            # Convert the old format into the new one and return a
            # uniform snaps dict
            for f in snaps:
                f['name'] = f['filesystem']  # Rename the key
                f.pop('filesystem', None)  # Delete the old key
        else:
            # Unify all Amazon regions and/or name variations to a single one
            if 'amazon' in cloud_name:
                cloud_name = 'amazon'
            for cloud in snaps_file['clouds']:
                if cloud_name == cloud['name'].lower():
                    current_cloud = cloud
                    for r in current_cloud['regions']:
                        if r['name'].lower() == self.app.cloud_interface.get_region_name().lower():
                            for d in r['deployments']:
                                # TODO: Make the deployment name a UD option
                                if d['name'] == 'GalaxyCloud':
                                    snaps = d['filesystems']

        log.debug("Loaded default snapshot data for cloud {1}: {0}".format(snaps,
            cloud_name))
        return snaps

    @TestFlag(10)
    def get_default_data_size(self):
        if not self.default_galaxy_data_size:
            for snap in self.snaps:
                roles = ServiceRole.from_string_array(snap['roles'])
                if ServiceRole.GALAXY_DATA in roles:
                    if 'size' in snap:
                        self.default_galaxy_data_size = snap['size']
                    elif 'snap_id' in snap:
                        self.snapshot = (self.app.cloud_interface.get_ec2_connection()
                            .get_all_snapshots([snap['snap_id']])[0])
                        self.default_galaxy_data_size = self.snapshot.volume_size
                    log.debug("Got default galaxy FS size as {0}GB".format(
                        self.default_galaxy_data_size))
        return str(self.default_galaxy_data_size)

    @TestFlag(False)
    def start(self):
        """
        This method is automatically called as CloudMan starts; it tries to add
        and start available cluster services (as provided in the cluster's
        configuration and persistent data).
        """
        log.debug("User Data at manager start, with secret_key and password filtered out: %s" %
                dict((k, self.app.ud[k]) for k in self.app.ud.keys() if k not in ['password', 'secret_key']))

        self._handle_prestart_commands()
        # Generating public key before any worker has been initialized
        # This is required for configuring Hadoop the main Hadoop worker still needs to be
        # bale to ssh into itself!!!
        # this should happen before SGE is added
        self.get_root_public_key()

       # Always add migration service
        self.add_master_service(MigrationService(self.app))

       # Always add SGE service
        self.add_master_service(SGEService(self.app))
        # Always share instance transient storage over NFS
        tfs = Filesystem(self.app, 'transient_nfs', svc_roles=[ServiceRole.TRANSIENT_NFS])
        tfs.add_transient_storage()
        self.add_master_service(tfs)
        # Always add PSS service - note that this service runs only after the cluster
        # type has been selected and all of the services are in RUNNING state
        self.add_master_service(PSSService(self.app))

        if self.app.config.condor_enabled:
            self.add_master_service(HTCondorService(self.app, "master"))
        # KWS: Optionally add Hadoop service based on config setting
        if self.app.config.hadoop_enabled:
            self.add_master_service(HadoopService(self.app))
        # Check if starting a derived cluster and initialize from share,
        # which calls add_preconfigured_services
        # Note that share_string overrides everything.
        if "share_string" in self.app.ud:
            # BUG TODO this currently happens on reboot, and shouldn't.
            self.init_shared_cluster(self.app.ud['share_string'].strip())
        # else look if this is a restart of a previously existing cluster
        # and add appropriate services
        elif not self.add_preconfigured_services():
            return False
        self.manager_started = True

        # Check if a previously existing cluster is being recreated or if it is a new one
        if not self.initial_cluster_type:  # this can get set by _handle_old_cluster_conf_format
            self.initial_cluster_type = self.app.ud.get('cluster_type', None)
            if self.initial_cluster_type is not None:
                cc_detail = "Configuring a previously existing cluster of type {0}"\
                    .format(self.initial_cluster_type)
            else:
                cc_detail = "This is a new cluster; waiting to configure the type."
                self.cluster_status = cluster_status.WAITING
        else:
            cc_detail = "Configuring an old existing cluster of type {0}"\
                .format(self.initial_cluster_type)
        log.info("Completed the initial cluster startup process. {0}".format(
            cc_detail))
        return True

    def handle_prestart_commands(self):
        """
        Inspect the user data key ``master_prestart_commands`` and simply
        execute any commands provided there.

        For example::
            master_prestart_commands:
              - "mkdir -p /mnt/galaxyData/pgsql/"
              - "mkdir -p /mnt/galaxyData/tmp"
              - "chown -R galaxy:galaxy /mnt/galaxyData"
        """
        for command in self.app.ud.get("master_prestart_commands", []):
            misc.run(command)

    @TestFlag(False)
    def add_preconfigured_services(self):
        """
        Inspect the cluster configuration and persistent data to add any
        previously defined cluster services.
        """
        log.debug("Checking for and adding any previously defined cluster services")
        return self.add_preconfigured_filesystems() and self.add_preconfigured_applications()

    def add_preconfigured_filesystems(self):
        try:
            # Process the current cluster config
            log.debug("Processing filesystems in an existing cluster config")
            attached_volumes = self.get_attached_volumes()
            if 'filesystems' in self.app.ud:
                for fs in self.app.ud.get('filesystems') or []:
                    err = False
                    filesystem = Filesystem(self.app, fs['name'], svc_roles=ServiceRole.from_string_array(
                        fs['roles']), mount_point=fs.get('mount_point', None))
                    # Based on the kind, add the appropriate file system. We can
                    # handle 'volume', 'snapshot', or 'bucket' kind
                    if fs['kind'] == 'volume':
                        if 'ids' not in fs and 'size' in fs:
                            # We're creating a new volume
                            filesystem.add_volume(size=fs['size'])
                        else:
                            # A volume already exists so use it
                            for vol_id in fs['ids']:
                                filesystem.add_volume(vol_id=vol_id)
                    elif fs['kind'] == 'snapshot':
                        for snap in fs['ids']:
                            # Check if an already attached volume maps to this snapshot
                            att_vol = self.get_vol_if_fs(attached_volumes, fs['name'])
                            if att_vol:
                                filesystem.add_volume(vol_id=att_vol.id, size=att_vol.size,
                                    from_snapshot_id=att_vol.snapshot_id)
                            else:
                                filesystem.add_volume(from_snapshot_id=snap)
                    elif fs['kind'] == 'nfs':
                        filesystem.add_nfs(fs['nfs_server'], None, None, mount_options=fs.get('mount_options', None))
                    elif fs['kind'] == 'gluster':
                        filesystem.add_glusterfs(fs['gluster_server'], mount_options=fs.get('mount_options', None))
                    elif fs['kind'] == 'transient':
                        filesystem.add_transient_storage(persistent=True)
                    elif fs['kind'] == 'bucket':
                        a_key = fs.get('access_key', None)
                        s_key = fs.get('secret_key', None)
                        # Can have only a single bucket per file system so
                        # access it directly
                        bucket_name = fs.get('ids', [None])[0]
                        if bucket_name:
                            filesystem.add_bucket(bucket_name, a_key, s_key)
                        else:
                            log.warning("No bucket name for file system {0}!".format(
                                fs['name']))
                    # TODO: include support for `nfs` kind
                    else:
                        # TODO: try to do some introspection on the device ID
                        # to guess the kind before err
                        err = True
                        log.warning("Device kind '{0}' for file system {1} not recognized; "
                                    "not adding the file system.".format(fs['kind'], fs['name']))
                    if not err:
                        log.debug("Adding a previously existing filesystem '{0}' of "
                                  "kind '{1}'".format(fs['name'], fs['kind']))
                        self.add_master_service(filesystem)
            return True
        except Exception, e:
            log.error(
                "Error processing filesystems in existing cluster configuration: %s" % e)
            self.manager_started = False
            return False

    def add_preconfigured_applications(self):
        """
        Dynamically add any previously available applications to the service
        registry, which will in turn start those apps. The service list is
        extracted from the user data.

        Note that this method is automatically called when an existing cluster
        is being recreated.

        In order for the dynamic service loading to work, there are some requirements
        on the structure of user data and services themselves. Namely, user data
        must contain a name for the service. The service implementation must be in
        a module inside ``cm.services.apps`` and it must match the service name
        (e.g., ``cm.services.apps.proftpd``). The provided service/file name must
        match the service class without the "Service" string. For example, if service
        name is ``ProFTPd``, file name must be proftpd.py and the service class
        name must be ``ProFTPdService``, properly capitilized.
        """
        try:
            ok = True  # Flag keeping up with progress
            # Process the current cluster config
            log.debug("Processing previously-available application services in "
                "an existing cluster config")
            # TODO: Should we inspect a provided path for service availability vs. UD only?
            if "services" in self.app.ud:
                log.debug("Previously-available applications: {0}"
                    .format(self.app.ud['services']))
                for srvc in self.app.ud['services']:
                    service_class = None
                    svc_name = srvc.get('name', None)
                    if svc_name:
                        # Import service module and get a reference to the service object
                        try:
                            module_name = svc_name.lower()
                            svc_name += "Service"  # Service class name must match this str!
                            module = __import__('cm.services.apps.' + module_name,
                                fromlist=[svc_name])
                            service_class = getattr(module, svc_name)
                        except Exception, e:
                            log.warning("Trouble importing service class {0}: {1}"
                                .format(svc_name, e))
                    if service_class:
                        # Add the service into the service registry
                        self.add_master_service(service_class(self.app))
                    else:
                        ok = False
                        log.warning("Could not find service class matching "
                            "userData service entry: %s" % svc_name)
            return ok
        except Exception, e:
            log.error("Error processing applications in existing cluster configuration: %s" % e)
            self.manager_started = False
            return False

    def get_vol_if_fs(self, attached_volumes, filesystem_name):
        """
        Iterate through the list of (attached) volumes and check if any
        one of them match the current cluster name and filesystem (as stored
        in volume's tags). Return a matching volume (as a ``boto`` object) or
        ``None``.

        *Note* that this method returns the first matching volume and will thus
        not work for filesystems composed of multiple volumes.
        """
        for vol in attached_volumes:
            log.debug("Checking if vol '{0}' is file system '{1}'".format(
                vol.id, filesystem_name))
            if self.app.cloud_interface.get_tag(vol, 'clusterName') == self.app.ud['cluster_name'] \
                    and self.app.cloud_interface.get_tag(vol, 'filesystem') == filesystem_name:
                log.debug("Identified attached volume '%s' as filesystem '%s'" % (
                    vol.id, filesystem_name))
                return vol
        return None

    def start_autoscaling(self, as_min, as_max, instance_type):
        as_svc = self.get_services(svc_role=ServiceRole.AUTOSCALE)
        if not as_svc:
            self.add_master_service(
                Autoscale(self.app, as_min, as_max, instance_type))
        else:
            log.debug("Autoscaling is already on.")
        as_svc = self.get_services(svc_role=ServiceRole.AUTOSCALE)
        log.debug(as_svc[0])

    def stop_autoscaling(self):
        as_svc = self.get_services(svc_role=ServiceRole.AUTOSCALE)
        if as_svc:
            self.remove_master_service(as_svc[0])
        else:
            log.debug("Not stopping autoscaling because it is not on.")

    def adjust_autoscaling(self, as_min, as_max):
        as_svc = self.get_services(svc_role=ServiceRole.AUTOSCALE)
        if as_svc:
            as_svc[0].as_min = int(as_min)
            as_svc[0].as_max = int(as_max)
            log.debug("Adjusted autoscaling limits; new min: %s, new max: %s" % (as_svc[
                      0].as_min, as_svc[0].as_max))
        else:
            log.debug(
                "Cannot adjust autoscaling because autoscaling is not on.")

    # DBTODO For now this is a quick fix to get a status.
    # Define what 'yellow' would be, and don't just count on "Filesystem"
    # being the only data service.
    def get_data_status(self):
        fses = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        if fses != []:
            for fs in fses:
                if fs.state == service_states.ERROR:
                    return "red"
                elif fs.state != service_states.RUNNING:
                    return "yellow"
            return "green"
        else:
            return "nodata"

    def get_app_status(self):
        count = 0
        for svc in self.get_services(svc_type=ServiceType.APPLICATION):
            count += 1
            if svc.state == service_states.ERROR:
                return "red"
            elif not (svc.state == service_states.RUNNING or svc.state == service_states.COMPLETED):
                return "yellow"
        if count != 0:
            return "green"
        else:
            return "nodata"

    def get_services(self, svc_type=None, svc_role=None, svc_name=None):
        """
        Returns all services that best match given service type, role and name.
        If service name is specified, it is matched first.
        Next, if a role is specified, returns all services containing that role.
        Lastly, if svc_role is ``None``, but a ``svc_type`` is specified, returns
        all services matching type.
        """
        svcs = []
        for s in self.services:
            if s.name is None:  # Sanity check
                log.error("A name has not been assigned to the service. A value must be assigned to the svc.name property.")
            elif s.name == svc_name:
                return [s]  # Only one match possible - so return it immediately
            elif svc_role in s.svc_roles:
                svcs.append(s)
            elif s.svc_type == svc_type and svc_role is None:
                svcs.append(s)

        return svcs

    def all_fs_status_text(self):
        return []
        # FIXME: unreachable code
        tr = []
        for key, vol in self.volumes.iteritems():
            if vol[3] is None:
                tr.append("%s+nodata" % key)
            else:
                if vol[3] is True:
                    tr.append("%s+green" % key)
                else:
                    tr.append("%s+red" % key)
        return tr

    def all_fs_status_array(self):
        return []
        # FIXME: unreachable code
        tr = []
        for key, vol in self.volumes.iteritems():
            if vol[3] is None:
                tr.append([key, "nodata"])
            else:
                if vol[3] is True:
                    tr.append([key, "green"])
                else:
                    tr.append([key, "red"])
        return tr

    def fs_status_text(self):
        """fs_status"""
        good_count = 0
        bad_count = 0
        fsarr = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        if len(fsarr) == 0:
            return "nodata"
        # DBTODO Fix this conflated volume/filesystem garbage.
        for fs in fsarr:
            if fs.state == service_states.RUNNING:
                good_count += 1
            else:
                bad_count += 1
        if good_count == len(fsarr):
            return "green"
        elif bad_count > 0:
            return "red"
        else:
            return "yellow"

    def pg_status_text(self):
        """pg_status"""
        svcarr = self.get_services(svc_role=ServiceRole.GALAXY_POSTGRES)
        if len(svcarr) > 0:
            if svcarr[0].state == service_states.RUNNING:
                return "green"
            else:
                return "red"
        else:
            return "nodata"

    def sge_status_text(self):
        """sge_status"""
        svcarr = self.get_services(svc_role=ServiceRole.SGE)
        if len(svcarr) > 0:
            if svcarr[0].state == service_states.RUNNING:
                return "green"
            else:
                return "red"
        else:
            return "nodata"

    def galaxy_status_text(self):
        """galaxy_status"""
        svcarr = self.get_services(svc_role=ServiceRole.GALAXY)
        if len(svcarr) > 0:
            if svcarr[0].state == service_states.RUNNING:
                return "green"
            else:
                return "red"
        else:
            return "nodata"

    def get_srvc_status(self, srvc):
        """
        Get the status a service ``srvc``. If the service is not a recognized as
        a CloudMan-service, return ``Service not recognized``. If the service is
        not currently running (i.e., not currently recognized by CloudMan as a
        service it should be managing), return ``Service not found``.
        """
        svcarr = self.get_services(svc_name=srvc)
        svcarr = [s for s in svcarr if (s.svc_type == ServiceType.FILE_SYSTEM or ServiceRole.fulfills_roles(
            s.svc_roles, [ServiceRole.GALAXY, ServiceRole.SGE, ServiceRole.GALAXY_POSTGRES]))]
        if len(svcarr) > 0:
            return srvc[0].state
        else:
            return "'%s' is not running" % srvc
        return "Service '%s' not recognized." % srvc

    @TestFlag([{"size_used": "184M", "status": "Running", "kind": "Transient",
                "mount_point": "/mnt/transient_nfs", "name": "transient_nfs", "err_msg": None,
                "device": "/dev/vdb", "size_pct": "1%", "DoT": "Yes", "size": "60G",
                "persistent": "No"},
               {"size_used": "33M", "status": "Running", "kind": "Volume",
                "mount_point": "/mnt/galaxyData", "name": "galaxyData", "snapshot_status": None,
                "err_msg": None, "snapshot_progress": None, "from_snap": "snap-galaxyFS",
                "volume_id": "vol-0000000d", "device": "/dev/vdc", "size_pct": "4%",
                "DoT": "No", "size": "1014M", "persistent": "Yes"},
               {"size_used": "52M", "status": "Configuring", "kind": "Volume",
                "mount_point": "/mnt/galaxyData", "name": "galaxyDataResize",
                "snapshot_status": "pending", "err_msg": None, "persistent": "Yes",
                "snapshot_progress": "10%", "from_snap": "snap-760fd33d",
                "volume_id": "vol-d5f3f9a9", "device": "/dev/sdh", "size_pct": "2%",
                "DoT": "No", "size": "5.0G"}], quiet=True)
    def get_all_filesystems_status(self):
        """
        Get a list and information about each of the file systems currently
        managed by CloudMan.
        """
        fss = []
        fs_svcs = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        for fs in fs_svcs:
            fss.append(fs.get_details())
        return fss

        # return []

        # TEMP only; used to alternate input on the UI
        # r = random.choice([1, 2, 3])
        r = 4
        log.debug("Dummy random #: %s" % r)
        dummy = [{"name": "galaxyData",
                  "status": "Running",
                  "device": "/dev/sdg1",
                  "kind": "volume",
                  "mount_point": "/mnt/galaxyData",
                  "DoT": "No",
                  "size": "20G",
                  "size_used": "2G",
                  "size_pct": "90%",
                  "error_msg": None,
                  "volume_id": "vol-dbi23ins"}]
        if r == 2 or r == 4:
            dummy.append(
                {"name": "1000g", "status": "Removing", "bucket_name": "1000genomes",
                 "kind": "bucket", "mount_point": "/mnt/100genomes", "DoT": "No",
                 "size": "N/A", "NFS_shared": True, "size_used": "", "size_pct": "", "error_msg": None})
        if r == 3:
            dummy[0]['status'] = "Adding"
        if r == 4:  # NGTODO: Hardcoded links below to tools and indices?
            dummy.append({"name": "galaxyTools", "status": "Available", "device": "/dev/sdg3",
            "kind": "snapshot", "mount_point": "/mnt/galaxyTools", "DoT": "Yes",
            "size": "10G", "size_used": "1.9G", "size_pct": "19%",
            "error_msg": None, "from_snap": "snap-bdr2whd"})
            dummy.append({"name": "galaxyIndices", "status": "Error", "device": "/dev/sdg2",
            "kind": "snapshot", "mount_point": "/mnt/galaxyIndices", "DoT": "Yes",
            "size": "700G", "NFS_shared": True, "size_used": "675G", "size_pct": "96%",
            "error_msg": "Process returned 2", "from_snap": "snap-89r23hd"})
            dummy.append({"name": "custom", "status": "Available", "device": "/dev/sdg4",
            "kind": "Volume", "mount_point": "/mnt/custom", "DoT": "No",
            "size": "70G", "NFS_shared": True, "size_used": "53G", "size_pct": "7%",
            "error_msg": ""})
        return dummy

    @TestFlag({"SGE": "Running", "Postgres": "Running", "Galaxy": "TestFlag",
               "Filesystems": "Running"}, quiet=True)
    def get_all_services_status(self):
        """
        Return a dictionary containing a list of currently running service and
        their status.

        For example::
            {"Postgres": "Running", "SGE": "Running", "Galaxy": "Running",
            "Filesystems": "Running"}
        """
        status_dict = {}
        for srvc in self.services:
            status_dict[srvc.name] = srvc.state  # NGTODO: Needs special handling for file systems
        return status_dict

    def get_galaxy_rev(self):
        """
        Get the Mercurial revision of the Galaxy instance that's running as a
        CloudMan-managed service.
        Return a string with either the revision (e.g., ``5757:963e73d40e24``)
        or ``N/A`` if unable to get the revision number.
        """
        cmd = "%s - galaxy -c \"cd %s; hg tip | grep -m 1 changeset | cut -d':' -f2,3\"" % (
            paths.P_SU, self.app.path_resolver.galaxy_home)
        process = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = process.communicate()
        if out[1] != '':
            rev = 'N/A'
        else:
            rev = out[0].strip()
        return rev

    def get_galaxy_admins(self):
        admins = 'None'
        try:
            config_file = open(os.path.join(
                self.app.path_resolver.galaxy_home, 'universe_wsgi.ini'), 'r').readlines()
            for line in config_file:
                if 'admin_users' in line:
                    admins = line.split('=')[1].strip()
                    break
        except IOError:
            pass
        return admins

    def get_permanent_storage_size(self):
        pss = 0
        fs_arr = self.get_services(svc_role=ServiceRole.GALAXY_DATA)
        for fs in fs_arr:
            for vol in fs.volumes:
                pss += int(vol.size)
        return pss

    def check_disk(self):
        try:
            fs_arr = self.get_services(svc_role=ServiceRole.GALAXY_DATA)
            if len(fs_arr) > 0:
                fs_name = fs_arr[0].name
                # NGTODO: Definite security issue here. After discussion with Enis, clients are considered trusted for now.
                # We may later have to think about sanitizing/securing/escaping user input if the issue arises.
                disk_usage = commands.getoutput("df -h | grep %s$ | awk '{print $2, $3, $5}'" % fs_name)
                disk_usage = disk_usage.split(' ')
                if len(disk_usage) == 3:
                    self.disk_total = disk_usage[0]
                    self.disk_used = disk_usage[1]
                    self.disk_pct = disk_usage[2]
        except Exception, e:
            log.error("Failure checking disk usage.  %s" % e)

    def get_cluster_status(self):
        return self.cluster_status

    def toggle_master_as_exec_host(self, force_removal=False):
        """ By default, the master instance running all the services is also
            an execution host and is used to run jobs. This method allows one
            to toggle the master instance from being an execution host.

            :type force_removal: bool
            :param force_removal: If True, go through the process of removing
                                  the instance from being an execution host
                                  irrespective of the instance's current state.

            :rtype: bool
            :return: True if the master instance is set to be an execution host.
                     False otherwise.
        """
        sge_svc = self.get_services(svc_role=ServiceRole.SGE)[0]
        if sge_svc.state == service_states.RUNNING:
            if self.master_exec_host is True or force_removal:
                self.master_exec_host = False
                if not sge_svc._remove_instance_from_exec_list(
                    self.app.cloud_interface.get_instance_id(),
                        self.app.cloud_interface.get_private_ip()):
                    # If the removal was unseccessful, reset the flag
                    self.master_exec_host = True
            else:
                self.master_exec_host = True
                if not sge_svc._add_instance_as_exec_host(
                    self.app.cloud_interface.get_instance_id(),
                        self.app.cloud_interface.get_private_ip()):
                    # If the removal was unseccessful, reset the flag
                    self.master_exec_host = False
        else:
            log.warning(
                "SGE not running thus cannot toggle master as exec host")
        if self.master_exec_host:
            log.info("The master instance has been set to execute jobs.  To manually change this, use the cloudman admin panel.")
        else:
            log.info("The master instance has been set to *not* execute jobs.  To manually change this, use the cloudman admin panel.")
        return self.master_exec_host

    def get_worker_instances(self):
        instances = []
        if self.app.TESTFLAG is True:
            # for i in range(5):
            #     instance = Instance( self.app, inst=None, m_state="Pending" )
            #     instance.id = "WorkerInstance"
            #     instances.append(instance)
            return instances
        log.debug(
            "Trying to discover any worker instances associated with this cluster...")
        filters = {'tag:clusterName': self.app.ud['cluster_name'],
                   'tag:role': 'worker'}
        try:
            reservations = self.app.cloud_interface.get_all_instances(filters=filters)
            for reservation in reservations:
                if reservation.instances[0].state != 'terminated' and reservation.instances[0].state != 'shutting-down':
                    i = Instance(self.app, inst=reservation.instances[0],
                                 m_state=reservation.instances[0].state, reboot_required=True)
                    instances.append(i)
                    log.info("Instance '%s' found alive (will configure it later)." % reservation.instances[0].id)
        except EC2ResponseError, e:
            log.debug("Error checking for live instances: %s" % e)
        return instances

    @TestFlag([])
    def get_attached_volumes(self):
        """
        Get a list of block storage volumes currently attached to this instance.
        """
        log.debug(
            "Trying to discover any volumes attached to this instance...")
        attached_volumes = []
        # TODO: Abstract filtering into the cloud interface classes
        try:
            if self.app.cloud_type == 'ec2':
                # filtering w/ boto is supported only with ec2
                f = {'attachment.instance-id':
                     self.app.cloud_interface.get_instance_id()}
                attached_volumes = self.app.cloud_interface.get_ec2_connection()\
                    .get_all_volumes(filters=f)
            else:
                volumes = self.app.cloud_interface.get_ec2_connection().get_all_volumes()
                for vol in volumes:
                    if vol.attach_data.instance_id == self.app.cloud_interface.get_instance_id():
                        attached_volumes.append(vol)
        except EC2ResponseError, e:
            log.debug("Error checking for attached volumes: %s" % e)
        log.debug("Attached volumes: %s" % attached_volumes)
        # Add ``clusterName`` tag to any attached volumes
        for att_vol in attached_volumes:
            self.app.cloud_interface.add_tag(att_vol, 'clusterName', self.app.ud['cluster_name'])
        return attached_volumes

    @TestFlag(None)
    def shutdown(self, sd_apps=True, sd_filesystems=True, sd_instances=True,
                 sd_autoscaling=True, delete_cluster=False, sd_spot_requests=True,
                 rebooting=False):
        """
        Shut down this cluster. This means shutting down all the services
        (dependent on method arguments) and, optionally, deleting the cluster.

        .. seealso:: `~cm.util.master.delete_cluster`
        """
        log.debug("List of services before shutdown: %s" % [
                  s.get_full_name() for s in self.services])
        self.cluster_status = cluster_status.SHUTTING_DOWN
        # Services need to be shut down in particular order
        if sd_autoscaling:
            self.stop_autoscaling()
        if sd_instances:
            self.stop_worker_instances()
        if sd_spot_requests:
            for wi in self.worker_instances:
                if wi.is_spot() and not wi.spot_was_filled():
                    wi.terminate()
        # full_svc_list = self.services[:]  # A copy to ensure consistency
        if sd_apps:
            for svc in self.get_services(svc_type=ServiceType.APPLICATION):
                log.debug("Initiating removal of service {0}".format(svc.name))
                svc.remove()
        if sd_filesystems:
            for svc in self.get_services(svc_type=ServiceType.FILE_SYSTEM):
                log.debug("Initiating removal of file system service {0}".format(svc.name))
                svc.remove(synchronous=True, delete_devices=delete_cluster)
        # Wait for all the services to shut down before declaring the cluster shut down
        # (but don't wait indefinitely)
        # This is required becasue with the file systems being removed in parallel via
        # separate threads, those processes may not have completed by the time the
        # complete shutdown does.
        time_limit = 300  # wait for max 5 mins before shutting down
        while(time_limit > 0):
            log.debug("Waiting ({0} more seconds) for all the services to shut down.".format(
                time_limit))
            num_off = 0
            for srvc in self.services:
                if srvc.state == service_states.SHUT_DOWN or srvc.state == service_states.ERROR or \
                        srvc.state == service_states.UNSTARTED:
                    num_off += 1
            if num_off == len(self.services):
                log.debug("All services shut down")
                break
            elif rebooting and self.app.cloud_type == 'ec2':
                # For the EC2 cloud it's ok to reboot with volumes attached
                log.debug("Not waiting for all the services to shut down because we're just rebooting.")
                break
            sleep_time = 6
            time.sleep(sleep_time)
            time_limit -= sleep_time
        if delete_cluster:
            self.delete_cluster()
        self.cluster_status = cluster_status.TERMINATED
        log.info("Cluster %s shut down at %s (uptime: %s). If not done automatically, "
            "manually terminate the master instance (and any remaining instances "
            "associated with this cluster) from the %s cloud console."
            % (self.app.ud['cluster_name'], Time.now(), (Time.now() - self.startup_time),
                self.app.ud.get('cloud_name', '')))

    def reboot(self, soft=False):
        """
        Reboot the entire cluster, first shutting down appropriate services.
        """
        if self.app.TESTFLAG is True:
            log.debug("Restart the cluster but the TESTFLAG is set")
            return False
        # Spot requests cannot be tagged and thus there is no good way of associating those
        # back with a cluster after a reboot so cancel those
        log.debug("Initiating cluster reboot.")
        # Don't detach volumes only on the EC2 cloud
        sd_filesystems = True
        if self.app.cloud_type == 'ec2':
            sd_filesystems = False
        self.shutdown(sd_filesystems=sd_filesystems, sd_instances=False, rebooting=True)
        if soft:
            if misc.run("{0} restart".format(os.path.join(self.app.ud['boot_script_path'],
               self.app.ud['boot_script_name']))):
                return True
            else:
                log.error(
                    "Trouble restarting CloudMan softly; rebooting instance now.")
        ec2_conn = self.app.cloud_interface.get_ec2_connection()
        try:
            log.debug("Rebooting self now...")
            ec2_conn.reboot_instances(
                [self.app.cloud_interface.get_instance_id()])
            return True
        except EC2ResponseError, e:
            log.error("Error rebooting master instance (i.e., self): %s" % e)
        return False

    def terminate_master_instance(self, delete_cluster=False):
        """
        Terminate the master instance using the cloud middleware API.
        If ``delete_cluster`` is set to ``True``, delete all cluster
        components before terminating the instance.

        .. seealso:: `~cm.util.master.delete_cluster`
        """
        if self.cluster_status != cluster_status.TERMINATED:
            self.shutdown(delete_cluster=delete_cluster)
        log.debug("Terminating the master instance")
        self.app.cloud_interface.terminate_instance(
            self.app.cloud_interface.get_instance_id())

    def delete_cluster(self):
        """
        Completely delete this cluster. This involves deleting the cluster's
        bucket as well as volumes containing user data file system(s)! The
        list of volumes to be deleted can either be provided as an argument or,
        for the case of EC2 only, will be automatically derived.

        .. warning::

            This action is irreversible. All data will be permanently deleted.

        """
        log.info("All services shut down; deleting this cluster.")
        # Delete any remaining volume(s) assoc. w/ the current cluster
        try:
            if self.app.cloud_type == 'ec2':
                filters = {'tag:clusterName': self.app.ud['cluster_name']}
                vols = self.app.cloud_interface.get_all_volumes(filters=filters)
                log.debug("Remaining volumes associated with this cluster: {0}".format(vols))
                for vol in vols:
                    if vol.status == 'available':
                        log.debug("As part of cluster deletion, deleting volume '%s'" % vol.id)
                        vol.delete()
                    else:
                        log.debug("Not deleting volume {0} because it is in state {1}"
                            .format(vol.id, vol.status))
        except EC2ResponseError, e:
            log.error("Error deleting a volume: %s" % e)
        # Delete cluster bucket on S3
        s3_conn = self.app.cloud_interface.get_s3_connection()
        if s3_conn:
            misc.delete_bucket(s3_conn, self.app.ud['bucket_cluster'])

    def clean(self):
        """
        Clean the system as if it was freshly booted. All services are shut down
        and any changes made to the system since service start are reverted (this
        excludes any data on user data file system).
        """
        log.debug("Cleaning the system - all services going down")
        # TODO: #NGTODO: Possibility of simply calling remove on ServiceType.FILE_SYSTEM
        # service so that all dependencies are automatically removed?
        svcs = self.get_services(svc_role=ServiceRole.GALAXY)
        for service in svcs:
            service.remove()
        svcs = self.get_services(svc_role=ServiceRole.GALAXY_POSTGRES)
        for service in svcs:
            service.remove()
        self.stop_worker_instances()
        svcs = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        for service in svcs:
            service.clean()
        svcs = self.get_services(svc_role=ServiceRole.SGE)
        for service in svcs:
            service.clean()

    def get_idle_instances(self):
        """
        Get a list of instances that are currently not executing any job manager
        jobs. Return a list of ``Instance`` objects.
        """
        # log.debug( "Looking for idle instances" )
        idle_instances = []  # List of Instance objects corresponding to idle instances
        if os.path.exists('%s/default/common/settings.sh' % self.app.path_resolver.sge_root):
            proc = subprocess.Popen("export SGE_ROOT=%s; . $SGE_ROOT/default/common/settings.sh; "
                                    "%s/bin/lx24-amd64/qstat -f | grep all.q" % (
                                        self.app.path_resolver.sge_root, self.app.path_resolver.sge_root),
                                    shell=True, stdout=subprocess.PIPE)
            qstat_out = proc.communicate()[0]
            # log.debug( "qstat output: %s" % qstat_out )
            instances = qstat_out.splitlines()
            nodes_list = []  # list of nodes containing node's domain name and number of used processing slots
            idle_instances_dn = []  # list of domain names of idle instances
            for inst in instances:
                # Get instance domain name and # of used processing slots: ['domU-12-31-38-00-48-D1.c:0']
                nodes_list.append(inst.split('@')[1].split(' ')[0] + ':' + inst.split('/')[1])
            # if len( nodes_list ) > 0:
            #     log.debug( "Processed qstat output: %s" % nodes_list )

            for node in nodes_list:
                # If number of used slots on given instance is 0, mark it as idle
                if int(node.split(':')[1]) == 0:
                    idle_instances_dn.append(node.split(':')[0])
            # if len( idle_instances_dn ) > 0:
            #     log.debug( "Idle instances' DNs: %s" % idle_instances_dn )

            for idle_instance_dn in idle_instances_dn:
                for w_instance in self.worker_instances:
                    # log.debug("Trying to match worker instance with private IP '%s' to idle "
                    #    "instance '%s'" % (w_instance.get_local_hostname(), idle_instance_dn))
                    if w_instance.get_local_hostname() is not None:
                        if w_instance.get_local_hostname().lower().startswith(str(idle_instance_dn).lower()):
                            # log.debug("Marking instance '%s' with FQDN '%s' as idle." \
                            #     % (w_instance.id, idle_instance_dn))
                            idle_instances.append(w_instance)
        return idle_instances

    def remove_instances(self, num_nodes, force=False):
        """
        Remove a number (``num_nodes``) of worker instances from the cluster, first
        deciding which instance(s) to terminate and then removing them from SGE and
        terminating. An instance is deemed removable if it is not currently running
        any jobs.

        Note that if the number of removable instances is smaller than the
        number of instances requested to remove, the smaller number of instances
        is removed. This can be overridden by setting ``force`` to ``True``. In that
        case, removable instances are removed first, then additional instances are
        chosen at random and removed.
        """
        num_terminated = 0
        # First look for idle instances that can be removed
        idle_instances = self.get_idle_instances()
        if len(idle_instances) > 0:
            log.debug("Found %s idle instances; trying to remove %s." %
                      (len(idle_instances), num_nodes))
            for i in range(0, num_nodes):
                for inst in idle_instances:
                    if num_terminated < num_nodes:
                        self.remove_instance(inst.id)
                        num_terminated += 1
        else:
            log.info("No idle instances found")
        log.debug("Num to terminate: %s, num terminated: %s; force set to '%s'"
                  % (num_nodes, num_terminated, force))
        # If force is set, terminate requested number of instances regardless
        # whether they are idle
        if force is True and num_terminated < num_nodes:
            force_kill_instances = num_nodes - num_terminated
            log.info(
                "Forcefully terminating %s instances." % force_kill_instances)
            for i in range(force_kill_instances):
                for inst in self.worker_instances:
                    if not inst.is_spot() or inst.spot_was_filled():
                        self.remove_instance(inst.id)
                        num_terminated += 1
        if num_terminated > 0:
            log.info("Initiated requested termination of instances. Terminating '%s' instances."
                     % num_terminated)
        else:
            log.info("Did not terminate any instances.")

    def remove_instance(self, instance_id=''):
        """
        Remove an instance with ID ``instance_id`` from the cluster. This means
        that the instance is first removed from the job manager as a worker and
        then it is terminated via cloud middleware API.
        """
        if instance_id == '':
            log.warning(
                "Tried to remove an instance but did not receive instance ID")
            return False
        log.debug("Specific termination of instance '%s' requested." % instance_id)
        for inst in self.worker_instances:
            if inst.id == instance_id:
                sge_svc = self.get_services(svc_role=ServiceRole.SGE)[0]
                # DBTODO Big problem here if there's a failure removing from allhosts.  Need to handle it.
                # if sge_svc.remove_sge_host(inst.get_id(), inst.get_private_ip()) is True:
                # Best-effort PATCH until above issue is handled
                if inst.get_id() is not None:
                    sge_svc.remove_sge_host(
                        inst.get_id(), inst.get_private_ip())
                    # Remove the given instance from /etc/hosts files
                    log.debug("Removing instance {0} from /etc/hosts".format(
                        inst.get_id()))
                    for line in fileinput.input('/etc/hosts', inplace=1):
                        line = line.strip()
                        # (print all lines except the one w/ instance IP back to the file)
                        if not inst.private_ip in line:
                            print line
                try:
                    inst.terminate()
                except EC2ResponseError, e:
                    log.error("Trouble terminating instance '{0}': {1}".format(
                        instance_id, e))
        log.info("Initiated requested termination of instance. Terminating '%s'." %
                 instance_id)

    def reboot_instance(self, instance_id='', count_reboot=True):
        """
        Using cloud middleware API, reboot instance with ID ``instance_id``.
        ``count_reboot`` indicates whether this count should be counted toward
        the instance ``self.config.instance_reboot_attempts`` (see `Instance`
        `reboot` method).
        """
        if instance_id == '':
            log.warning("Tried to reboot an instance but did not receive instance ID")
            return False
        log.info("Specific reboot of instance '%s' requested." % instance_id)
        for inst in self.worker_instances:
            if inst.id == instance_id:
                inst.reboot(count_reboot=count_reboot)
        log.info("Initiated requested reboot of instance. Rebooting '%s'." % instance_id)

    def add_instances(self, num_nodes, instance_type='', spot_price=None):
        # Remove master from execution queue automatically
        if self.master_exec_host:
            self.toggle_master_as_exec_host()
        self.app.cloud_interface.run_instances(num=num_nodes,
                                               instance_type=instance_type,
                                               spot_price=spot_price)

    def add_live_instance(self, instance_id):
        """
        Add an existing instance to the list of worker instances tracked by the master;
        get a handle to the instance object in the process.
        """
        try:
            log.debug("Adding live instance '%s'" % instance_id)
            reservation = self.app.cloud_interface.get_all_instances(instance_id)
            if reservation and len(reservation[0].instances) == 1:
                instance = reservation[0].instances[0]
                if instance.state != 'terminated' and instance.state != 'shutting-down':
                    i = Instance(self.app, inst=instance, m_state=instance.state)
                    self.app.cloud_interface.add_tag(instance, 'clusterName',
                        self.app.ud['cluster_name'])
                    # Default to 'worker' role tag
                    self.app.cloud_interface.add_tag(instance, 'role', 'worker')
                    self.app.cloud_interface.add_tag(instance, 'Name',
                        "Worker: {0}".format(self.app.ud['cluster_name']))
                    self.worker_instances.append(i)
                    i.send_alive_request()  # to make sure info like ip-address and hostname are updated
                    log.debug('Added instance {0}....'.format(instance_id))
                else:
                    log.debug("Live instance '%s' is at the end of its life (state: %s); not adding the instance." %
                              (instance_id, instance.state))
                return True
        except EC2ResponseError, e:
            log.debug(
                "Problem adding a live instance (tried ID: %s): %s" % (instance_id, e))
        return False

    @TestFlag(None)
    def init_cluster(self, cluster_type, pss=0, storage_type='volume'):
        """
        Initialize the type for this cluster and start appropriate services,
        storing the cluster configuration into the cluster's bucket.

        This method applies only to a new cluster.

        :type cluster_type: string
        :param cluster_type: Type of cluster being setup. Currently, accepting
                             values ``Galaxy``, ``Data``, or ``SGE``

        :type pss: int
        :param pss: Persistent Storage Size associated with data volumes being
                    created for the cluster
        """
        def _add_data_fs():
            """
            A local convenience method used to add a new file system
            """
            if self.get_services(svc_role=ServiceRole.GALAXY_DATA):
                return
            fs_name = ServiceRole.to_string(ServiceRole.GALAXY_DATA)
            log.debug("Creating a new data filesystem: '%s'" % fs_name)
            fs = Filesystem(
                self.app, fs_name, svc_roles=[ServiceRole.GALAXY_DATA])
            fs.add_volume(size=pss)
            self.add_master_service(fs)

        self.cluster_status = cluster_status.STARTING
        self.initial_cluster_type = cluster_type
        msg = "Initializing '{0}' cluster type with storage type '{1}'. Please wait...".format(cluster_type, storage_type)
        log.info(msg)
        self.app.msgs.info(msg)
        if cluster_type == 'Galaxy':
            # Turn those data sources into file systems
            if self.snaps:
                attached_volumes = self.get_attached_volumes()
                for snap in [s for s in self.snaps if 'name' in s]:
                    if 'roles' in snap:
                        fs = Filesystem(self.app, snap['name'],
                             svc_roles=ServiceRole.from_string_array(snap['roles']))
                        # Check if an already attached volume maps to the current filesystem
                        att_vol = self.get_vol_if_fs(attached_volumes, snap['name'])
                        if att_vol:
                            log.debug("{0} file system has volume(s) already attached".format(
                                snap['name']))
                            fs.add_volume(vol_id=att_vol.id,
                                          size=att_vol.size, from_snapshot_id=att_vol.snapshot_id)
                            # snap_size = att_vol.size
                        elif 'snap_id' in snap:
                            log.debug("There are no volumes already attached for file system {0}"
                                .format(snap['name']))
                            size = 0
                            if ServiceRole.GALAXY_DATA in ServiceRole.from_string_array(snap['roles']):
                                size = pss
                            fs.add_volume(size=size, from_snapshot_id=snap['snap_id'])
                            # snap_size = snap.get('size', 0)
                        elif 'type' in snap:
                            if 'archive' == snap['type'] and 'archive_url' in snap:
                                log.debug("Attaching a volume based on an archive named {0}"
                                    .format(snap['name']))
                                if storage_type == 'volume':
                                    if 'size' in snap:
                                        size = snap['size']
                                        if ServiceRole.GALAXY_DATA in ServiceRole.from_string_array(snap['roles']):
                                            if pss > snap['size']:
                                                size = pss
                                        from_archive = {'url': snap['archive_url'],
                                            'md5_sum': snap.get('archive_md5', None)}
                                        fs.add_volume(size=size, from_archive=from_archive)
                                    else:
                                        log.error("Format error in snaps.yaml file. No size specified for volume based on archive {0}"
                                                  .format(snap['name']))
                                elif storage_type == 'transient':
                                    from_archive = {'url': snap['archive_url'],
                                        'md5_sum': snap.get('archive_md5', None)}
                                    fs.add_transient_storage(from_archive=from_archive)
                                else:
                                    log.error("Unknown storage type {0} for archive extraction."
                                              .format(storage_type))
                            elif 'gluster' == snap['type'] and 'server' in snap:
                                log.debug("Attaching a glusterfs based filesystem named {0}"
                                    .format(snap['name']))
                                fs.add_glusterfs(snap['server'], mount_options=snap.get('mount_options', None))
                            elif 'nfs' == snap['type'] and 'server' in snap:
                                log.debug("Attaching an nfs based filesystem named {0}"
                                    .format(snap['name']))
                                fs.add_nfs(snap['server'], None, None, mount_options=snap.get('mount_options', None))
                            elif 's3fs' == snap['type'] and 'bucket_name' in snap and 'bucket_a_key' in snap and 'bucket_s_key' in snap:
                                fs.add_bucket(snap['bucket_name'], snap['bucket_a_key'], snap['bucket_s_key'])
                            else:
                                log.error("Format error in snaps.yaml file. Unrecognised or improperly configured type '{0}' for fs named: {1}"
                                    .format(snap['type]'], snap['name']))
                        log.debug("Adding a filesystem '{0}' with volumes '{1}'"
                            .format(fs.get_full_name(), fs.volumes))
                        self.add_master_service(fs)
            # Add a file system for user's data
            if self.app.use_volumes:
                _add_data_fs()
            # Add PostgreSQL service
            self.add_master_service(PostgresService(self.app))
            # Add ProFTPd service
            self.add_master_service(ProFTPdService(self.app))
            # Add Galaxy service
            self.add_master_service(GalaxyService(self.app))
            # Add Galaxy Reports service
            self.add_master_service(GalaxyReportsService(self.app))
        elif cluster_type == 'Data':
            # Add a file system for user's data if one doesn't already exist
            _add_data_fs()
        elif cluster_type == 'SGE':
            # SGE service is automatically added at cluster start (see
            # ``start`` method)
            pass
        else:
            log.error("Tried to initialize a cluster but received an unknown type: '%s'"
                % cluster_type)

    @TestFlag(True)
    @synchronized(s3_rlock)
    def init_shared_cluster(self, share_string):
        """
        Initialize a new (i.e., derived) cluster from a shared one, whose details
        need to be provided in the ``share_string`` (e.g.,
        ``cm-808d863548acae7c2328c39a90f52e29/shared/2012-09-17--19-47``)

        This method can only be called at a new cluster start.
        """
        self.cluster_status = cluster_status.STARTING
        log.debug("Initializing a shared cluster from '%s'" % share_string)
        s3_conn = self.app.cloud_interface.get_s3_connection()
        ec2_conn = self.app.cloud_interface.get_ec2_connection()
        try:
            share_string = share_string.strip('/')
            bucket_name = share_string.split('/')[0]
            cluster_config_prefix = os.path.join(
                share_string.split('/')[1], share_string.split('/')[2])
        except Exception, e:
            log.error("Error while parsing provided shared cluster's bucket '%s': %s" % (
                share_string, e))
            return False
        # Check that the shared cluster's bucket exists
        if not misc.bucket_exists(s3_conn, bucket_name, validate=False):
            log.error(
                "Shared cluster's bucket '%s' does not exist or is not accessible!" % bucket_name)
            return False
        # Create the new cluster's bucket
        if not misc.bucket_exists(s3_conn, self.app.ud['bucket_cluster']):
            misc.create_bucket(s3_conn, self.app.ud['bucket_cluster'])
        # Copy contents of the shared cluster's bucket to the current cluster's
        # bucket
        fl = "shared_instance_file_list.txt"
        if misc.get_file_from_bucket(s3_conn, bucket_name,
                os.path.join(cluster_config_prefix, fl), fl, validate=False):
            key_list = misc.load_yaml_file(fl)
            for key in key_list:
                misc.copy_file_in_bucket(
                    s3_conn, bucket_name, self.app.ud['bucket_cluster'],
                    key, key.split('/')[-1], preserve_acl=False, validate=False)
        else:
            log.error("Problem copying shared cluster configuration files. Cannot continue with "
                      "the shared cluster initialization.")
            return False
        # Create a volume from shared cluster's data snap and set current
        # cluster's data volume
        shared_cluster_pd_file = 'shared_p_d.yaml'
        if misc.get_file_from_bucket(
            s3_conn, self.app.ud['bucket_cluster'], 'persistent_data.yaml',
                shared_cluster_pd_file):
            scpd = misc.load_yaml_file(shared_cluster_pd_file)
            self.initial_cluster_type = scpd.get('cluster_type', None)
            log.debug("Initializing %s cluster type from shared cluster" % self.initial_cluster_type)
            if 'shared_data_snaps' in scpd:
                shared_data_vol_snaps = scpd['shared_data_snaps']
                try:
                    # TODO: If support for multiple volumes comprising a file system becomes available,
                    # this code will need to adjusted to accommodate that. Currently, the assumption is
                    # that only 1 snap ID will be provided as the data file
                    # system.
                    snap = ec2_conn.get_all_snapshots(shared_data_vol_snaps)[0]
                    # Create a volume here because we'll be dealing with a volume-based file system
                    # and for that we need a volume ID
                    data_vol = ec2_conn.create_volume(
                        snap.volume_size, self.app.cloud_interface.get_zone(),
                        snapshot=snap)
                    # Old style for persistent data - delete if the other method works as expected
                    # scpd['data_filesystems'] = {'galaxyData': [{'vol_id': data_vol.id, 'size': data_vol.size}]}
                    # Compose a persistent_data compatible entry for the shared data volume so that
                    # the appropriate file system can be created as part of ``add_preconfigured_services``
                    # TODO: make it more general vs. galaxy specific
                    data_fs_yaml = {'ids': [data_vol.id],
                                    'kind': 'volume',
                                    'mount_point': '/mnt/galaxy',
                                    'name': 'galaxy',
                                    'roles': ['galaxyTools', 'galaxyData']}
                    scpd['filesystems'].append(data_fs_yaml)
                    log.info("Created a data volume '%s' of size %sGB from shared cluster's snapshot '%s'"
                             % (data_vol.id, data_vol.size, snap.id))
                    # Don't make the new cluster shared by default
                    del scpd['shared_data_snaps']
                    # Update new cluster's persistent_data.yaml
                    cc_file_name = 'cm_cluster_config.yaml'
                    log.debug("Dumping scpd to file {0} (which will become persistent_data.yaml): {1}"
                              .format(cc_file_name, scpd))
                    misc.dump_yaml_to_file(scpd, cc_file_name)
                    misc.save_file_to_bucket(
                        s3_conn, self.app.ud[
                            'bucket_cluster'], 'persistent_data.yaml',
                        cc_file_name)
                except EC2ResponseError, e:
                    log.error("EC2 error creating volume from shared cluster's snapshot '%s': %s"
                              % (shared_data_vol_snaps, e))
                    return False
                except Exception, e:
                    log.error("Error creating volume from shared cluster's snapshot '%s': %s"
                              % (shared_data_vol_snaps, e))
                    return False
            else:
                log.error("Loaded configuration from the shared cluster does not have a reference "
                          "to a shared data snapshot. Cannot continue.")
                return False
        # TODO: Reboot the instance so CloudMan source downloaded from the shared
        # instance is used
        # log.info("Rebooting the cluster so shared instance source can be reloaded.")
        # self.reboot(soft=True)
        # Reload user data and start the cluster as normally would
        self.app.ud = self.app.cloud_interface.get_user_data(force=True)
        if misc.get_file_from_bucket(s3_conn, self.app.ud['bucket_cluster'], 'persistent_data.yaml', 'pd.yaml'):
            pd = misc.load_yaml_file('pd.yaml')
            self.app.ud = misc.merge_yaml_objects(self.app.ud, pd)
        reload(paths)  # Must reload because paths.py might have changes in it
        self.add_preconfigured_services()
        return True

    @TestFlag({})
    @synchronized(s3_rlock)
    def share_a_cluster(self, user_ids=None, canonical_ids=None):
        """
        Setup the environment to make the current cluster shared (via a shared
        volume snapshot).
        This entails stopping all services to enable creation of a snapshot of
        the data volume, allowing others to create a volume from the created
        snapshot as well giving read permissions to cluster's bucket. If user_ids
        are not provided, the bucket and the snapshot are made public.

        :type user_ids: list
        :param user_ids: The numeric Amazon IDs of users (with no dashes) to
                         give read permissions to the bucket and snapshot

        :type canonical_ids: list
        :param canonical_ids: A list of Amazon Canonical IDs (in the same linear
                              order as the ``user_ids``) that will be used to
                              enable sharing of individual objects in the
                              cluster's bucket.
        """
        # TODO: rewrite this to use > 3 character variable names.
        # TODO: recover services if the process fails midway
        log.info("Setting up the cluster for sharing")
        self.cluster_manipulation_in_progress = True
        self._stop_app_level_services()

        # Initiate snapshot of the galaxyData file system
        snap_ids = []
        svcs = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        for svc in svcs:
            if ServiceRole.GALAXY_DATA in svc.svc_roles:
                snap_ids = svc.create_snapshot(snap_description="CloudMan share-a-cluster %s; %s"
                                        % (self.app.ud['cluster_name'], self.app.ud['bucket_cluster']))
        # Create a new folder-like structure inside cluster's bucket and copy
        # the cluster configuration files
        s3_conn = self.app.cloud_interface.get_s3_connection()
        # All of the shared cluster's config files will be stored with the
        # specified prefix
        shared_names_root = "shared/%s" % Time.now().strftime("%Y-%m-%d--%H-%M")
        # Create current cluster config and save it to cluster's shared location,
        # including the freshly generated snap IDs
        conf_file_name = 'cm_shared_cluster_conf.yaml'
        addl_data = {'shared_data_snaps': snap_ids}
        self.console_monitor.create_cluster_config_file(
            conf_file_name, addl_data=addl_data)
        # Remove references to cluster's own data; this is shared via the snapshots above
        # TODO: Add an option for a user to include any self-added file systems
        # as well
        sud = misc.load_yaml_file(conf_file_name)
        fsl = sud.get('filesystems', [])
        sfsl = []  # Shared file systems list
        for fs in fsl:
            roles = ServiceRole.from_string_array(fs['roles'])
            # Including GALAXY_TOOLS role here breaks w/ new combined galaxyData/galaxyTools volume.  We should
            # probably change this to actually inspect and share base snapshots if applicable (like galaxyIndices) but
            # never volumes.
            # if ServiceRole.GALAXY_TOOLS in roles or ServiceRole.GALAXY_INDICES in roles:
            if ServiceRole.GALAXY_INDICES in roles:
                sfsl.append(fs)
        sud['filesystems'] = sfsl
        misc.dump_yaml_to_file(sud, conf_file_name)
        misc.save_file_to_bucket(s3_conn, self.app.ud['bucket_cluster'],
                                 os.path.join(shared_names_root, 'persistent_data.yaml'), conf_file_name)
        # Keep track of which keys were copied into the shared folder
        copied_key_names = [os.path.join(shared_names_root,
                                         'persistent_data.yaml')]
        # Save the remaining cluster configuration files
        try:
            # Get a list of all files stored in cluster's bucket excluding
            # any keys that include '/' (i.e., are folders) or the previously
            # copied 'persistent_data.yaml'. This way, if the number of config
            # files changes in the future, this will still work
            b = s3_conn.lookup(self.app.ud['bucket_cluster'])
            keys = b.list(delimiter='/')
            conf_files = []
            for key in keys:
                if '/' not in key.name and 'persistent_data.yaml' not in key.name:
                    conf_files.append(key.name)
        except S3ResponseError, e:
            log.error("Error collecting cluster configuration files form bucket '%s': %s"
                      % (self.app.ud['bucket_cluster'], e))
            return False
        # Copy current cluster's configuration files into the shared folder
        for conf_file in conf_files:
            if 'clusterName' not in conf_file:  # Skip original cluster name file
                misc.copy_file_in_bucket(s3_conn,
                                         self.app.ud['bucket_cluster'],
                                         self.app.ud['bucket_cluster'],
                                         conf_file, os.path.join(
                                             shared_names_root, conf_file),
                                         preserve_acl=False)
                copied_key_names.append(
                    os.path.join(shared_names_root, conf_file))
        # Save the list of files contained in the shared bucket so derivative
        # instances can know what to get with minimim permissions
        fl = "shared_instance_file_list.txt"
        misc.dump_yaml_to_file(copied_key_names, fl)
        misc.save_file_to_bucket(s3_conn, self.app.ud['bucket_cluster'], os.path.join(shared_names_root, fl), fl)
        copied_key_names.append(os.path.join(shared_names_root, fl))  # Add it to the list so it's permissions get set
        # Adjust permissions on the new keys and the created snapshots
        ec2_conn = self.app.cloud_interface.get_ec2_connection()
        for snap_id in snap_ids:
            try:
                if user_ids:
                    log.debug(
                        "Adding createVolumePermission for snap '%s' for users '%s'" % (snap_id, user_ids))
                    ec2_conn.modify_snapshot_attribute(
                        snap_id, attribute='createVolumePermission',
                        operation='add', user_ids=user_ids)
                else:
                    ec2_conn.modify_snapshot_attribute(
                        snap_id, attribute='createVolumePermission',
                        operation='add', groups=['all'])
            except EC2ResponseError, e:
                log.error(
                    "Error modifying snapshot '%s' attribute: %s" % (snap_id, e))
        err = False
        if canonical_ids:
            # In order to list the keys associated with a shared instance, a user
            # must be given READ permissions on the cluster's bucket as a whole.
            # This allows a given user to list the contents of a bucket but not
            # access any of the keys other than the ones granted the permission
            # next (i.e., keys required to bootstrap the shared instance)
            # misc.add_bucket_user_grant(s3_conn, self.app.ud['bucket_cluster'], 'READ', canonical_ids, recursive=False)
            # Grant READ permissions for the keys required to bootstrap the
            # shared instance
            for k_name in copied_key_names:
                if not misc.add_key_user_grant(s3_conn, self.app.ud['bucket_cluster'], k_name, 'READ', canonical_ids):
                    log.error(
                        "Error adding READ permission for key '%s'" % k_name)
                    err = True
        else:  # If no canonical_ids are provided, means to set the permissions to public-read
            # See above, but in order to access keys, the bucket root must be given read permissions
            # FIXME: this method sets the bucket's grant to public-read and
            # removes any individual user's grants - something share-a-cluster
            # depends on down the line if the publicly shared instance is deleted
            # misc.make_bucket_public(s3_conn, self.app.ud['bucket_cluster'])
            for k_name in copied_key_names:
                if not misc.make_key_public(s3_conn, self.app.ud['bucket_cluster'], k_name):
                    log.error("Error making key '%s' public" % k_name)
                    err = True
        if err:
            # TODO: Handle this with more user input?
            log.error("Error modifying permissions for keys in bucket '%s'" %
                      self.app.ud['bucket_cluster'])

        self._start_app_level_services()
        self.cluster_manipulation_in_progress = False
        return True

    @synchronized(s3_rlock)
    def get_shared_instances(self):
        """
        Get a list of point-in-time shared instances of this cluster.
        Returns a list such instances. Each element of the returned list is a
        dictionary with ``bucket``, ``snap``, and ``visibility`` keys.
        """
        lst = []
        if self.app.TESTFLAG is True:
            lst.append({"bucket": "cm-7834hdoeiuwha/TESTshare/2011-08-14--03-02/", "snap":
                       'snap-743ddw12', "visibility": 'Shared'})
            lst.append({"bucket": "cm-7834hdoeiuwha/TESTshare/2011-08-19--10-49/", "snap":
                       'snap-gf69348h', "visibility": 'Public'})
            return lst
        try:
            s3_conn = self.app.cloud_interface.get_s3_connection()
            b = misc.get_bucket(s3_conn, self.app.ud['bucket_cluster'])
            if b:
                # Get a list of shared 'folders' containing clusters'
                # configuration
                folder_list = b.list(prefix='shared/', delimiter='/')
                for folder in folder_list:
                    # Get snapshot assoc. with the current shared cluster
                    tmp_pd = 'tmp_pd.yaml'
                    if misc.get_file_from_bucket(
                        s3_conn, self.app.ud['bucket_cluster'],
                            os.path.join(folder.name, 'persistent_data.yaml'), tmp_pd):
                        tmp_ud = misc.load_yaml_file(tmp_pd)
                        # Currently, only a single volume snapshot can be associated
                        # a shared instance so pull it out of the list
                        if 'shared_data_snaps' in tmp_ud and len(tmp_ud['shared_data_snaps']) == 1:
                            snap_id = tmp_ud['shared_data_snaps'][0]
                        else:
                            snap_id = "Missing-ERROR"
                        try:
                            os.remove(tmp_pd)
                        except OSError:
                            pass  # Best effort temp file cleanup
                    else:
                        snap_id = "Missing-ERROR"
                    # Get permission on the persistent_data file and assume
                    # the entire cluster shares those permissions
                    k = b.get_key(
                        os.path.join(folder.name, 'persistent_data.yaml'))
                    if k is not None:
                        acl = k.get_acl()
                        if 'AllUsers' in str(acl):
                            visibility = 'Public'
                        else:
                            visibility = 'Shared'
                        lst.append(
                            {"bucket": os.path.join(self.app.ud['bucket_cluster'],
                                                    folder.name), "snap": snap_id, "visibility": visibility})
        except S3ResponseError, e:
            log.error(
                "Problem retrieving references to shared instances: %s" % e)
        return lst

    @synchronized(s3_rlock)
    def delete_shared_instance(self, shared_instance_folder, snap_id):
        """
        Deletes all files under shared_instance_folder (i.e., all keys with
        ``shared_instance_folder`` prefix) and ``snap_id``, thus deleting the
        shared instance of the given cluster.

        :type shared_instance_folder: str
        :param shared_instance_folder: Prefix for the shared cluster instance
            configuration (e.g., ``shared/2011-02-24--20-52/``)

        :type snap_id: str
        :param snap_id: Snapshot ID to be deleted (e.g., ``snap-04c01768``)
        """
        if self.app.TESTFLAG is True:
            log.debug("Tried deleting shared instance for folder '%s' and snap '%s' but TESTFLAG is set." % (
                shared_instance_folder, snap_id))
            return True
        log.debug("Calling delete shared instance for folder '%s' and snap '%s'" % (shared_instance_folder, snap_id))
        ok = True  # Mark if encountered error but try to delete as much as possible
        try:
            s3_conn = self.app.cloud_interface.get_s3_connection()
            # Revoke READ grant for users associated with the instance
            # being deleted but do so only if the given users do not have
            # access to any other shared instances.
            # users_whose_grant_to_remove = misc.get_users_with_grant_on_only_this_folder(s3_conn, self.app.ud['bucket_cluster'], shared_instance_folder)
            # if len(users_whose_grant_to_remove) > 0:
            #     misc.adjust_bucket_ACL(s3_conn, self.app.ud['bucket_cluster'], users_whose_grant_to_remove)
            # Remove keys and folder associated with the given shared instance
            b = misc.get_bucket(s3_conn, self.app.ud['bucket_cluster'])
            key_list = b.list(prefix=shared_instance_folder)
            for key in key_list:
                log.debug(
                    "As part of shared cluster instance deletion, deleting key '%s' from bucket '%s'" % (key.name,
                                                                                                         self.app.ud['bucket_cluster']))
                key.delete()
        except S3ResponseError, e:
            log.error("Problem deleting keys in '%s': %s" % (
                shared_instance_folder, e))
            ok = False
        # Delete the data snapshot associated with the shared instance being
        # deleted
        try:
            ec2_conn = self.app.cloud_interface.get_ec2_connection()
            ec2_conn.delete_snapshot(snap_id)
            log.debug(
                "As part of shared cluster instance deletion, deleted snapshot '%s'" % snap_id)
        except EC2ResponseError, e:
            log.error(
                "As part of shared cluster instance deletion, problem deleting snapshot '%s': %s" % (snap_id, e))
            ok = False
        return ok

    def update_file_system(self, file_system_name):
        """ This method is used to update the underlying EBS volume/snapshot
        that is used to hold the provided file system. This is useful when
        changes have been made to the underlying file system and those changes
        wish to be preserved beyond the runtime of the current instance. After
        calling this method, terminating and starting the cluster instance over
        will preserve any changes made to the file system (provided the snapshot
        created via this method has not been deleted).
        The method performs the following steps:
        1. Suspend all application-level services
        2. Unmount and detach the volume associated with the file system
        3. Create a snapshot of the volume
        4. Delete references to the original file system's EBS volume
        5. Add a new reference to the created snapshot, which gets picked up
           by the monitor and a new volume is created and file system mounted
        6. Unsuspend services
        """
        if self.app.TESTFLAG is True:
            log.debug("Attempted to update file system '%s', but TESTFLAG is set." % file_system_name)
            return None
        log.info("Initiating file system '%s' update." % file_system_name)
        self.cluster_manipulation_in_progress = True
        self._stop_app_level_services()

        # Initiate snapshot of the specified file system
        snap_ids = []
        svcs = self.get_services(svc_type=ServiceType.FILE_SYSTEM)
        found_fs_name = False  # Flag to ensure provided fs name was actually found
        for svc in svcs:
            if svc.name == file_system_name:
                found_fs_name = True
                # Create a snapshot of the given volume/file system
                snap_ids = svc.create_snapshot(snap_description="File system '%s' from CloudMan instance '%s'; bucket: %s"
                                        % (file_system_name, self.app.ud['cluster_name'], self.app.ud['bucket_cluster']))
                # Remove the old volume by removing the entire service
                if len(snap_ids) > 0:
                    log.debug("Removing file system '%s' service as part of the file system update"
                              % file_system_name)
                    svc.remove()
                    log.debug("Creating file system '%s' from snaps '%s'" % (file_system_name, snap_ids))
                    fs = Filesystem(self.app, file_system_name, svc.svc_roles)
                    for snap_id in snap_ids:
                        fs.add_volume(from_snapshot_id=snap_id)
                    self.add_master_service(fs)
                    # Monitor will pick up the new service and start it up but
                    # need to wait until that happens before can add rest of
                    # the services
                    while fs.state != service_states.RUNNING:
                        log.debug("Service '%s' not quite ready: '%s'" % (
                            fs.get_full_name(), fs.state))
                        time.sleep(6)
        if found_fs_name:
            self._start_app_level_services()
            self.cluster_manipulation_in_progress = False
            log.info("File system '%s' update complete" % file_system_name)
            return True
        else:
            log.error("Did not find file system with name '%s'; update not performed." %
                      file_system_name)
            return False

    def add_fs_bucket(self, bucket_name, fs_name=None, fs_roles=[ServiceRole.GENERIC_FS],
                      bucket_a_key=None, bucket_s_key=None, persistent=False):
        """
        Add a new file system service for a bucket-based file system.
        """
        log.info("Adding a {4} file system {3} from bucket {0} (w/ creds {1}:{2})"
                 .format(bucket_name, bucket_a_key, bucket_s_key, fs_name, persistent))
        fs = Filesystem(self.app, fs_name or bucket_name,
                        persistent=persistent, svc_roles=fs_roles)
        fs.add_bucket(bucket_name, bucket_a_key, bucket_s_key)
        self.add_master_service(fs)
        # Inform all workers to add the same FS (the file system will be the same
        # and sharing it over NFS does not seems to work)
        for w_inst in self.worker_instances:
            w_inst.send_add_s3fs(bucket_name, fs_roles)
        log.debug("Master done adding FS from bucket {0}".format(bucket_name))

    @TestFlag(None)
    def add_fs_volume(self, fs_name, fs_kind, vol_id=None, snap_id=None, vol_size=0,
                      fs_roles=[ServiceRole.GENERIC_FS], persistent=False, dot=False):
        """
        Add a new file system based on an existing volume, a snapshot, or a new
        volume. Provide ``fs_kind`` to distinguish between these (accepted values
        are: ``volume``, ``snapshot``, or ``new_volume``). Depending on which
        kind is provided, must provide ``vol_id``, ``snap_id``, or ``vol_size``,
        respectively - but not all!
        """
        log.info("Adding a {0}-based file system '{1}'".format(fs_kind, fs_name))
        fs = Filesystem(self.app, fs_name, persistent=persistent, svc_roles=fs_roles)
        fs.add_volume(vol_id=vol_id, size=vol_size, from_snapshot_id=snap_id, dot=dot)
        self.add_master_service(fs)
        log.debug("Master done adding {0}-based FS {1}".format(fs_kind, fs_name))

    @TestFlag(None)
    def add_fs_gluster(self, gluster_server, fs_name,
                       fs_roles=[ServiceRole.GENERIC_FS], persistent=False):
        """
        Add a new file system service for a Gluster-based file system.
        """
        log.info("Adding a Gluster-based file system {0} from Gluster server {1}".format(fs_name, gluster_server))
        fs = Filesystem(self.app, fs_name, persistent=persistent, svc_roles=fs_roles)
        fs.add_glusterfs(gluster_server)
        self.add_master_service(fs)
        # Inform all workers to add the same FS (the file system will be the same
        # and sharing it over NFS does not seems to work)
        for w_inst in self.worker_instances:
            # w_inst.send_add_nfs_fs(nfs_server, fs_name, fs_roles, username, pwd)
            w_inst.send_mount_points()
        log.debug("Master done adding FS from Gluster server {0}".format(gluster_server))

    @TestFlag(None)
    def add_fs_nfs(self, nfs_server, fs_name, username=None, pwd=None,
                   fs_roles=[ServiceRole.GENERIC_FS], persistent=False):
        """
        Add a new file system service for a NFS-based file system. Optionally,
        provide password-based credentials (``username`` and ``pwd``) for
        accessing the NFS server.
        """
        log.info("Adding a NFS-based file system {0} from NFS server {1}".format(fs_name, nfs_server))
        fs = Filesystem(self.app, fs_name, persistent=persistent, svc_roles=fs_roles)
        fs.add_nfs(nfs_server, username, pwd)
        self.add_master_service(fs)
        # Inform all workers to add the same FS (the file system will be the same
        # and sharing it over NFS does not seems to work)
        for w_inst in self.worker_instances:
            # w_inst.send_add_nfs_fs(nfs_server, fs_name, fs_roles, username, pwd)
            w_inst.send_mount_points()
        log.debug("Master done adding FS from NFS server {0}".format(nfs_server))

    def stop_worker_instances(self):
        """
        Initiate termination of all worker instances.
        """
        log.info("Stopping all '%s' worker instance(s)" % len(
            self.worker_instances))
        to_terminate = []
        for i in self.worker_instances:
            to_terminate.append(i)
        for inst in to_terminate:
            log.debug(
                "Initiating termination of instance %s" % inst.get_desc())
            inst.terminate()
            # log.debug("Initiated termination of instance '%s'" % inst.id )

    @TestFlag({})  # {'default_CM_rev': '64', 'user_CM_rev':'60'} # For testing
    @synchronized(s3_rlock)
    def check_for_new_version_of_CM(self):
        """
        Check revision metadata for CloudMan (CM) in user's bucket and the default CM bucket.

        :rtype: dict
        :return: A dictionary with 'default_CM_rev' and 'user_CM_rev' keys where each key
                 maps to an string representation of an int that corresponds to the version of
                 CloudMan in the default repository vs. the currently running user's version.
                 If CloudMan is unable to determine the versions, an empty dict is returned.
        """
        log.debug("Checking for new version of CloudMan")
        s3_conn = self.app.cloud_interface.get_s3_connection()
        user_CM_rev = misc.get_file_metadata(
            s3_conn, self.app.ud['bucket_cluster'], self.app.config.cloudman_source_file_name, 'revision')
        default_CM_rev = misc.get_file_metadata(
            s3_conn, self.app.ud['bucket_default'], self.app.config.cloudman_source_file_name, 'revision')
        log.debug("Revision number for user's CloudMan: '%s'; revision number for default CloudMan: '%s'" %
                  (user_CM_rev, default_CM_rev))
        if user_CM_rev and default_CM_rev:
            try:
                if int(default_CM_rev) > int(user_CM_rev):
                    return {'default_CM_rev': default_CM_rev, 'user_CM_rev': user_CM_rev}
            except Exception:
                pass
        return {}

    @synchronized(s3_rlock)
    def update_users_CM(self):
        """
        If the revision number of CloudMan (CM) source file (as stored in file's metadata)
        in user's bucket is less than that of default CM, upload the new version of CM to
        user's bucket. Note that the update will take effect only after the next cluster reboot.

        :rtype: bool
        :return: If update was successful, return True.
                 Else, return False
        """
        if self.app.TESTFLAG is True:
            log.debug("Attempted to update CM, but TESTFLAG is set.")
            return None
        if self.check_for_new_version_of_CM():
            log.info("Updating CloudMan application source file in the cluster's bucket '%s'. "
                "It will be automatically available the next time this cluster is instantiated."
                % self.app.ud['bucket_cluster'])
            s3_conn = self.app.cloud_interface.get_s3_connection()
            # Make a copy of the old/original CM source and boot script in the cluster's bucket
            # called 'copy_name' and 'copy_boot_name', respectively
            copy_name = "%s_%s" % (
                self.app.config.cloudman_source_file_name, dt.date.today())
            copy_boot_name = "%s_%s" % (
                self.app.ud['boot_script_name'], dt.date.today())
            if misc.copy_file_in_bucket(s3_conn, self.app.ud['bucket_cluster'],
                                        self.app.ud['bucket_cluster'], self.app.config.cloudman_source_file_name, copy_name) and \
                misc.copy_file_in_bucket(
                    s3_conn, self.app.ud['bucket_cluster'],
                    self.app.ud['bucket_cluster'], self.app.ud['boot_script_name'], copy_boot_name):
                # Now copy CloudMan source from the default bucket to cluster's bucket as
                # self.app.config.cloudman_source_file_name and cm_boot.py as
                # 'boot_script_name'
                if misc.copy_file_in_bucket(
                    s3_conn, self.app.ud['bucket_default'],
                    self.app.ud[
                        'bucket_cluster'], self.app.config.cloudman_source_file_name,
                    self.app.config.cloudman_source_file_name) and misc.copy_file_in_bucket(s3_conn,
                                                                                            self.app.ud[
                        'bucket_default'], self.app.ud['bucket_cluster'],
                        'cm_boot.py', self.app.ud['boot_script_name']):
                    return True
        return False

    def expand_user_data_volume(self, new_vol_size, fs_name, snap_description=None,
                                delete_snap=False):
        """
        Mark the file system ``fs_name`` for size expansion. For full details on how
        this works, take a look at the file system expansion method for the
        respective file system type.
        If the underlying file system supports/requires creation of a point-in-time
        snapshot, setting ``delete_snap`` to ``False`` will retain the snapshot
        that will be creted during the expansion process under the given cloud account.
        If the snapshot is to be kept, a brief ``snap_description`` can be provided.
        """
        # Match fs_name with a service or if it's null or empty, default to
        # GALAXY_DATA role
        if fs_name:
            svcs = self.app.manager.get_services(svc_name=fs_name)
            if svcs:
                svc = svcs[0]
            else:
                log.error("Could not initiate expansion of {0} file system because "
                    "the file system was not found?".format(fs_name))
                return
        else:
            svc = self.app.manager.get_services(
                svc_role=ServiceRole.GALAXY_DATA)[0]

        log.debug("Marking '%s' for expansion to %sGB with snap description '%s'"
                  % (svc.get_full_name(), new_vol_size, snap_description))
        svc.state = service_states.CONFIGURING
        svc.grow = {
            'new_size': new_vol_size, 'snap_description': snap_description,
            'delete_snap': delete_snap}

    @TestFlag('TESTFLAG_ROOTPUBLICKEY')
    def get_root_public_key(self):
        """
        Generate or retrieve a public ssh key for the user running CloudMan and
        return it as a string. The key file is stored in ``id_rsa.pub``.
        Also, the private portion of the key is copied to ``/root/.ssh/id_rsa``
        to enable passwordless login by job manager jobs.
        """
        if self.root_pub_key is None:
            if not os.path.exists('id_rsa'):
                log.debug("Generating root user's public key...")
                ret_code = subprocess.call('ssh-keygen -t rsa -N "" -f id_rsa', shell=True)
                if ret_code == 0:
                    log.debug("Successfully generated root user's public key.")
                    f = open('id_rsa.pub')
                    self.root_pub_key = f.readline()
                    f.close()
                    # Must copy private key at least to /root/.ssh for
                    # passwordless login to work
                    shutil.copy2('id_rsa', '/root/.ssh/id_rsa')
                    log.debug(
                        "Successfully retrieved root user's public key from file.")
                else:
                    log.error("Encountered a problem while creating root user's public key, process returned error code '%s'." % ret_code)
            else:  # This is master restart, so
                f = open('id_rsa.pub')
                self.root_pub_key = f.readline()
                f.close()
                if not os.path.exists('/root/.ssh/id_rsa'):
                    # Must copy private key at least to /root/.ssh for passwordless login to work
                    shutil.copy2('id_rsa', '/root/.ssh/id_rsa')
                log.info("Successfully retrieved root user's public key from file.")
        return self.root_pub_key

    @TestFlag(None)
    def save_host_cert(self, host_cert):
        """
        Save host certificate ``host_cert`` to ``/root/.ssh/knowns_hosts``
        """
        log.debug("Saving host certificate '%s'" % host_cert)
        log.debug("Saving worker host certificate.")
        f = open("/root/.ssh/known_hosts", 'a')
        f.write(host_cert)
        f.close()
        return True

    def get_workers_status(self, worker_id=None):
        """
        Retrieves current status of all worker instances or of only worker
        instance whose ID was passed as the parameter. Returns a dict
        where instance ID's are the keys.
        """
        if self.app.TESTFLAG is True:
            log.debug("Attempted to get worker status, but TESTFLAG is set.")
            return {}
        workers_status = {}
        if worker_id:
            log.info("Checking status of instance '%s'" % worker_id)
            try:
                reservation = self.app.cloud_interface.get_all_instances(worker_id.strip())
                if reservation:
                    workers_status[reservation[0]
                                   .instances[0].id] = reservation[0].instances[0].state
            except Exception, e:
                log.error("Error while updating instance '%s' status: %s" % (worker_id, e))
        else:
            logging.info("Checking status of all worker nodes... ")
            for w_instance in self.worker_instances:
                workers_status[w_instance.id] = w_instance.get_m_state()
        return workers_status

    def get_num_available_workers(self):
        """
        Return the number of available worker nodes. A worker node is assumed
        available if it is in state ``READY``.
        """
        # log.debug("Gathering number of available workers" )
        num_available_nodes = 0
        for inst in self.worker_instances:
            if inst.node_ready is True:
                num_available_nodes += 1
        return num_available_nodes

    # ==========================================================================
    # ============================ UTILITY METHODS =============================
    # ========================================================================
    @synchronized(s3_rlock)
    def _make_file_from_list(self, input_list, file_name, bucket_name=None):
        """
        Create a file from provided list so that each list element is
        printed on a separate line. If bucket_name parameter is provided,
        save created file to the bucket.

        :rtype: bool
        :return: True if a file was created and, if requested by provding
        bucket name, successfully saved to the bucket. False if length of
        provided list is 0 or bucket save fails.
        """
        if len(input_list) > 0:
            with open(file_name, 'w') as f:
                for el in input_list:
                    f.write("%s\n" % el)
            if bucket_name is not None:
                log.debug("Saving file '%s' created from list '%s' to user's bucket '%s'." %
                          (file_name, input_list, bucket_name))
                s3_conn = self.app.cloud_interface.get_s3_connection()
                return misc.save_file_to_bucket(s3_conn, bucket_name, file_name, file_name)
        else:
            log.debug("Will not create file '%s' from provided list because the list is empty." %
                      file_name)
            return False
        return True

    def _update_file(self, file_name, search_exp, replace_exp):
        """
        Search file_name for a line containing search_exp and replace that
        expression with replace_exp.

        :type file_name: str
        :param file_name: Name of the file to modify

        :type search_exp: str
        :param search_exp: String for which to search

        :type replace_exp: str
        :param replace_exp: String used to replace search string
        """
        fin = open(file_name)
        fout = open("%s-tmp" % file_name, "w")
        for line in fin:
            fout.write(line.replace(search_exp, replace_exp))
        fin.close()
        fout.close()
        shutil.copy("%s-tmp" % file_name, file_name)

    def update_etc_host(self):
        """
        This method is for syncing hosts files in all workers with the master.
        It will copy the master etc hosts into a shared folder and send a message
        to the workers to inform them of the change.
        """
        shutil.copy("/etc/hosts", paths.P_ETC_TRANSIENT_PATH)
        for wrk in self.worker_instances:
            wrk.send_sync_etc_host(paths.P_ETC_TRANSIENT_PATH)

    def update_condor_host(self, new_worker_ip):
        """
        Add the new pool to the condor big pool
        """
        srvs = self.get_services(svc_role=ServiceRole.HTCONDOR)
        if srvs:
            srvs[0].modify_htcondor("ALLOW_WRITE", new_worker_ip)

    def get_status_dict(self):
        """
        Return a status dictionary for the current instance.

        The dictionary includes the following keys: ``id`` of the instance;
        ``ld`` as a load of the instance over the past 1, 5, and 15 minutes
        (e.g., ``0.00 0.02 0.39``); ``time_in_state`` as the length of time
        since instance state was last changed; ``instance_type`` as the type
        of the instance provisioned by the cloud; and ``public_ip`` with the
        public IP address of the instance.
        """
        public_ip = self.app.cloud_interface.get_public_ip()
        if self.app.TESTFLAG:
            num_cpus = 1
            load = "0.00 0.02 0.39"
            return {'id': 'localtest', 'ld': load,
                    'time_in_state': misc.formatSeconds(Time.now() - self.startup_time),
                    'instance_type': 'tester', 'public_ip': public_ip}
        else:
            num_cpus = int(commands.getoutput("cat /proc/cpuinfo | grep processor | wc -l"))
            load = (commands.getoutput("cat /proc/loadavg | cut -d' ' -f1-3")).strip()  # Returns system load in format "0.00 0.02 0.39" for the past 1, 5, and 15 minutes, respectively
        if load != 0:
            lds = load.split(' ')
            if len(lds) == 3:
                load = "%s %s %s" % (float(lds[0]) / int(num_cpus), float(
                    lds[1]) / int(num_cpus), float(lds[2]) / int(num_cpus))
            else:
                # Debug only, this should never happen.  If the interface is
                # able to display this, there is load.
                load = "0 0 0"
        return {'id': self.app.cloud_interface.get_instance_id(), 'ld': load, 'time_in_state': misc.formatSeconds(Time.now() - self.startup_time), 'instance_type': self.app.cloud_interface.get_type(), 'public_ip': public_ip}


class ConsoleMonitor(object):
    def __init__(self, app):
        self.app = app
        self.num_workers_processed = 0
        self.sge_was_setup = False
        self.last_state_change_time = None
        self.conn = comm.CMMasterComm()
        if not self.app.TESTFLAG:
            self.conn.setup()
        self.sleeper = misc.Sleeper()
        self.running = True
        # Keep some local stats to be able to adjust system updates
        self.last_update_time = Time.now()
        self.last_system_change_time = Time.now()
        self.update_frequency = 10  # Frequency (in seconds) between system updates
        self.num_workers = -1
        # Start the monitor thread
        self.monitor_thread = threading.Thread(target=self.__monitor)

    def start(self):
        """
        Start the monitor thread, which monitors and manages all the services
        visible to CloudMan.
        """
        self.last_state_change_time = Time.now()
        if not self.app.TESTFLAG:
            # Set 'role' and 'clusterName' tags for the master instance
            try:
                i_id = self.app.cloud_interface.get_instance_id()
                ir = self.app.cloud_interface.get_all_instances(i_id)
                self.app.cloud_interface.add_tag(
                    ir[0].instances[0], 'clusterName', self.app.ud['cluster_name'])
                self.app.cloud_interface.add_tag(
                    ir[0].instances[0], 'role', self.app.ud['role'])
                self.app.cloud_interface.add_tag(ir[0].instances[0], 'Name',
                    "{0}: {1}".format(self.app.ud['role'], self.app.ud['cluster_name']))
            except Exception, e:
                log.debug("Error setting tags on the master instance: %s" % e)
        self.monitor_thread.start()

    def shutdown(self):
        """
        Attempts to gracefully shut down the monitor thread, in turn stopping
        system updates.
        """
        log.info("Monitor received stop signal")
        try:
            log.info("Sending stop signal to the Monitor thread")
            if self.conn:
                self.conn.shutdown()
            self.running = False
            self.sleeper.wake()
            log.info("ConsoleMonitor thread stopped")
        except:
            pass

    def _update_frequency(self):
        """ Update the frequency value at which system updates are performed by the monitor.
        """
        # Check if a worker was added/removed since the last update
        if self.num_workers != len(self.app.manager.worker_instances):
            self.last_system_change_time = Time.now()
            self.num_workers = len(self.app.manager.worker_instances)
        # Update frequency: as more time passes since a change in the system,
        # progressivley back off on frequency of system updates
        if (Time.now() - self.last_system_change_time).seconds > 600:
            self.update_frequency = 60  # If no system changes for 10 mins, run update every minute
        elif (Time.now() - self.last_system_change_time).seconds > 300:
            self.update_frequency = 30  # If no system changes for 5 mins, run update every 30 secs
        else:
            self.update_frequency = 10  # If last system change within past 5 mins, run update every 10 secs

    def update_instance_sw_state(self, inst_id, state):
        """
        :type inst_id: string
        :type state: string
        """
        log.debug("Updating local ref to instance '%s' state to '%s'" % (inst_id, state))
        for inst in self.app.manager.worker_instances:
            if inst.id == inst_id:
                inst.sw_state = state

    def expand_user_data_volume(self):
        # TODO: recover services if process fails midway
        log.info("Initiating user data volume resizing")
        self.app.manager._stop_app_level_services()

        # Grow galaxyData filesystem
        svcs = self.app.manager.get_services(svc_type=ServiceType.FILE_SYSTEM)
        for svc in svcs:
            if ServiceRole.GALAXY_DATA in svc.svc_roles:
                log.debug("Expanding '%s'" % svc.get_full_name())
                svc.expand()

        self.app.manager._start_app_level_services()
        return True

    def create_cluster_config_file(self, file_name='persistent_data-current.yaml', addl_data=None):
        """
        Capture the current cluster configuration in a file (i.e., ``persistent_data.yaml``
        in cluster's bucket). The generated file is stored in CloudMan's running
        directory as ``file_name``. If provided, ``addl_data`` is included in
        the created configuration file.
        """
        try:
            cc = {}  # cluster configuration
            svcs = []  # list of services
            fss = []  # list of filesystems
            if addl_data:
                cc = addl_data
            cc['tags'] = self.app.cloud_interface.tags  # save cloud tags, in case the cloud doesn't support them natively
            for srvc in self.app.manager.services:
                if srvc.svc_type == ServiceType.FILE_SYSTEM:
                    if srvc.persistent:
                        fs = {}
                        fs['name'] = srvc.name
                        fs['roles'] = ServiceRole.to_string_array(srvc.svc_roles)
                        fs['mount_point'] = srvc.mount_point
                        fs['kind'] = srvc.kind
                        if srvc.kind == 'bucket':
                            fs['ids'] = [b.bucket_name for b in srvc.buckets]
                            fs['access_key'] = b.a_key
                            fs['secret_key'] = b.s_key
                        elif srvc.kind == 'volume':
                            fs['ids'] = [v.volume_id for v in srvc.volumes]
                        elif srvc.kind == 'snapshot':
                            fs['ids'] = [
                                v.from_snapshot_id for v in srvc.volumes]
                        elif srvc.kind == 'nfs':
                            fs['nfs_server'] = srvc.nfs_fs.device
                            fs['mount_options'] = srvc.nfs_fs.mount_options
                        elif srvc.kind == 'gluster':
                            fs['gluster_server'] = srvc.gluster_fs.device
                            fs['mount_options'] = srvc.gluster_fs.mount_options
                        elif srvc.kind == 'transient':
                            pass
                        else:
                            log.error("For filesystem {0}, unknown kind: {0}"
                                .format(srvc.name, srvc.kind))
                        fss.append(fs)
                else:
                    s = {}
                    s['name'] = srvc.name
                    s['roles'] = ServiceRole.to_string_array(srvc.svc_roles)
                    if ServiceRole.GALAXY in srvc.svc_roles:
                        s['home'] = self.app.path_resolver.galaxy_home
                    if ServiceRole.AUTOSCALE in srvc.svc_roles:
                        # We do not persist Autoscale service
                        pass
                    else:
                        svcs.append(s)
            cc['filesystems'] = fss
            cc['services'] = svcs
            cc['cluster_type'] = self.app.manager.initial_cluster_type
            cc['cluster_name'] = self.app.ud['cluster_name']
            cc['placement'] = self.app.cloud_interface.get_zone()
            cc['machine_image_id'] = self.app.cloud_interface.get_ami()
            cc['persistent_data_version'] = self.app.PERSISTENT_DATA_VERSION
            # If 'deployment_version' is not in UD, don't store it in the config
            if 'deployment_version' in self.app.ud:
                cc['deployment_version'] = self.app.ud['deployment_version']
            misc.dump_yaml_to_file(cc, file_name)
            # Reload the user data object in case anything has changed
            self.app.ud = misc.merge_yaml_objects(cc, self.app.ud)
        except Exception, e:
            log.error("Problem creating cluster configuration file: '%s'" % e)
        return file_name

    @synchronized(s3_rlock)
    def store_cluster_config(self):
        """
        Create a cluster configuration file and store it into cluster's bucket under name
        ``persistent_data.yaml``. The cluster configuration is considered the set of currently
        seen services in the master.

        In addition, store the local Galaxy configuration files to the cluster's
        bucket (do so only if they are not already there).
        """
        log.debug("Storing cluster configuration to cluster's bucket")
        s3_conn = self.app.cloud_interface.get_s3_connection()
        if not s3_conn:
            # s3_conn will be None is use_object_store is False, in this case just skip this
            # function.
            return
        if not misc.bucket_exists(s3_conn, self.app.ud['bucket_cluster']):
            misc.create_bucket(s3_conn, self.app.ud['bucket_cluster'])
        # Save/update the current Galaxy cluster configuration to cluster's
        # bucket
        cc_file_name = self.create_cluster_config_file()
        misc.save_file_to_bucket(s3_conn, self.app.ud['bucket_cluster'],
                                 'persistent_data.yaml', cc_file_name)
        # Ensure Galaxy config files are stored in the cluster's bucket,
        # but only after Galaxy has been configured and is running (this ensures
        # that the configuration files get loaded from proper S3 bucket rather
        # than potentially being overwritten by files that might exist on the
        # snap)
        try:
            galaxy_svc = self.app.manager.get_services(
                svc_role=ServiceRole.GALAXY)[0]
            if galaxy_svc.running():
                for f_name in ['universe_wsgi.ini',
                               'tool_conf.xml',
                               'tool_data_table_conf.xml',
                               'shed_tool_conf.xml',
                               'datatypes_conf.xml']:
                    if (os.path.exists(os.path.join(self.app.path_resolver.galaxy_home, f_name))) or \
                       (misc.file_in_bucket_older_than_local(s3_conn, self.app.ud['bucket_cluster'], '%s.cloud' % f_name, os.path.join(self.app.path_resolver.galaxy_home, f_name))):
                        log.debug(
                            "Saving current Galaxy configuration file '%s' to cluster bucket '%s' as '%s.cloud'" % (f_name,
                                                                                                                    self.app.ud['bucket_cluster'], f_name))
                        misc.save_file_to_bucket(
                            s3_conn, self.app.ud[
                                'bucket_cluster'], '%s.cloud' % f_name,
                            os.path.join(self.app.path_resolver.galaxy_home, f_name))
        except:
            pass
        # If not existent, save current boot script cm_boot.py to cluster's bucket
        # BUG: workaround eucalyptus Walrus, which hangs on returning saved file status if misc.file_exists_in_bucket() called first
        # if not misc.file_exists_in_bucket(s3_conn,
        # self.app.ud['bucket_cluster'], self.app.ud['boot_script_name']) and
        # os.path.exists(os.path.join(self.app.ud['boot_script_path'],
        # self.app.ud['boot_script_name'])):
        log.debug("Saving current instance boot script (%s) to cluster bucket '%s' as '%s'" % (os.path.join(self.app.ud['boot_script_path'], self.app.ud['boot_script_name']
                                                                                                            ), self.app.ud['bucket_cluster'], self.app.ud['boot_script_name']))
        misc.save_file_to_bucket(s3_conn, self.app.ud['bucket_cluster'], self.app.ud['boot_script_name'], os.path.join(self.app.ud[
                                 'boot_script_path'], self.app.ud['boot_script_name']))
        # If not existent, save CloudMan source to cluster's bucket, including file's metadata
        # BUG : workaround eucalyptus Walrus, which hangs on returning saved file status if misc.file_exists_in_bucket() called first
        # if not misc.file_exists_in_bucket(s3_conn,
        # self.app.ud['bucket_cluster'], 'cm.tar.gz') and
        # os.path.exists(os.path.join(self.app.ud['cloudman_home'],
        # 'cm.tar.gz')):
        log.debug("Saving CloudMan source (%s) to cluster bucket '%s' as '%s'" % (
            os.path.join(self.app.ud['cloudman_home'], 'cm.tar.gz'), self.app.ud['bucket_cluster'], 'cm.tar.gz'))
        misc.save_file_to_bucket(
            s3_conn, self.app.ud['bucket_cluster'], 'cm.tar.gz',
            os.path.join(self.app.ud['cloudman_home'], 'cm.tar.gz'))
        try:
            # Currently, metadata only works on ec2 so set it only there
            if self.app.cloud_type == 'ec2':
                with open(os.path.join(self.app.ud['cloudman_home'], 'cm_revision.txt'), 'r') as rev_file:
                    rev = rev_file.read()
                misc.set_file_metadata(s3_conn, self.app.ud[
                    'bucket_cluster'], 'cm.tar.gz', 'revision', rev)
        except Exception, e:
            log.debug("Error setting revision metadata on newly copied cm.tar.gz in bucket %s: %s" % (self.app.ud[
                      'bucket_cluster'], e))
        # Create an empty file whose name is the name of this cluster (useful
        # as a reference)
        cn_file = os.path.join(self.app.ud['cloudman_home'],
                               "%s.clusterName" % self.app.ud['cluster_name'])
        # BUG : workaround eucalyptus Walrus, which hangs on returning saved file status if misc.file_exists_in_bucket() called first
        # if not misc.file_exists_in_bucket(s3_conn,
        # self.app.ud['bucket_cluster'], "%s.clusterName" %
        # self.app.ud['cluster_name']):
        with open(cn_file, 'w'):
            pass
        if os.path.exists(cn_file):
            log.debug("Saving '%s' file to cluster bucket '%s' as '%s.clusterName'" % (
                cn_file, self.app.ud['bucket_cluster'], self.app.ud['cluster_name']))
            misc.save_file_to_bucket(s3_conn, self.app.ud['bucket_cluster'],
                                     "%s.clusterName" % self.app.ud['cluster_name'], cn_file)

    def __add_services(self):
        # Check and add any new services
        added_srvcs = False  # Flag to indicate if cluster conf was changed
        for service in [s for s in self.app.manager.services if s.state == service_states.UNSTARTED]:
            log.debug("Monitor adding service '%s'" % service.get_full_name())
            self.last_system_change_time = Time.now()
            if service.add():
                added_srvcs = True  # else:

            # log.debug("Monitor DIDN'T add service {0}? Service state: {1}"\
            # .format(service.get_full_name(), service.state))
            # Store cluster conf after all services have been added.
            # NOTE: this flag relies on the assumption service additions are
            # sequential (i.e., monitor waits for the service add call to complete).
            # If any of the services are to be added via separate threads, a
            # system-wide flag should probably be maintained for that particular
            # service that would indicate the configuration of the service is
            # complete. This could probably be done by monitoring
            # the service state flag that is already maintained?
        if added_srvcs and self.app.cloud_type != 'opennebula':
            self.store_cluster_config()  # Check and grow the file system
        svcs = self.app.manager.get_services(svc_type=ServiceType.FILE_SYSTEM)
        for svc in svcs:
            if ServiceRole.GALAXY_DATA in svc.svc_roles and svc.grow is not None:
                self.last_system_change_time = Time.now()
                self.expand_user_data_volume()
            # Opennebula has no storage like S3, so this is not working (yet)
                if self.app.cloud_type != 'opennebula':
                    self.store_cluster_config()

    def __check_amqp_messages(self):
        # Check for any new AMQP messages
        m = self.conn.recv()
        while m is not None:
            def do_match():
                match = False
                for inst in self.app.manager.worker_instances:
                    if str(inst.id) == str(m.properties['reply_to']):
                        match = True
                        inst.handle_message(m.body)
                return match

            if not do_match():
                log.debug("No instance (%s) match found for message %s; will add instance now!"
                    % (m.properties['reply_to'], m.body))
                if self.app.manager.add_live_instance(m.properties['reply_to']):
                    do_match()
                else:
                    log.warning("Potential error, got message from instance '%s' "
                                "but not aware of this instance. Ignoring the instance."
                                % m.properties['reply_to'])
            m = self.conn.recv()

    def __monitor(self):
        if not self.app.manager.manager_started:
            if not self.app.manager.start():
                log.critical("\n\n***** Manager failed to start *****\n")
                return False
        log.debug("Monitor started; manager started")
        while self.running:
            self.sleeper.sleep(4)
            if self.app.manager.cluster_status == cluster_status.TERMINATED:
                self.running = False
                return
            # In case queue connection was not established, try again (this will happen if
            # RabbitMQ does not start in time for CloudMan)
            if not self.conn.is_connected():
                log.debug(
                    "Trying to setup AMQP connection; conn = '%s'" % self.conn)
                self.conn.setup()
                continue
            # Do a periodic system state update (eg, services, workers)
            self._update_frequency()
            if (Time.now() - self.last_update_time).seconds > self.update_frequency:
                self.last_update_time = Time.now()
                self.app.manager.check_disk()
                for service in self.app.manager.services:
                    service.status()
                # Indicate migration is in progress
                migration_service = self.app.manager.get_services(svc_role=ServiceRole.MIGRATION)
                if migration_service:
                    migration_service = migration_service[0]
                    msg = "Migration service in progress; please wait."
                    if migration_service.state == service_states.RUNNING:
                        if not self.app.msgs.message_exists(msg):
                            self.app.msgs.critical(msg)
                    elif migration_service.state == service_states.COMPLETED:
                        self.app.msgs.remove_message(msg)
                # Log current services' states (in condensed format)
                svcs_state = "S&S: "
                for s in self.app.manager.services:
                    svcs_state += "%s..%s; " % (s.get_full_name(),
                        'OK' if s.state == 'Running' else s.state)
                log.debug(svcs_state)
                # Check the status of worker instances
                for w_instance in self.app.manager.worker_instances:
                    if w_instance.is_spot():
                        w_instance.update_spot()
                        if not w_instance.spot_was_filled():
                            # Wait until the Spot request has been filled to start
                            # treating the instance as a regular Instance
                            continue
                    # Send current mount points to ensure master and workers FSs are in sync
                    if w_instance.node_ready:
                        w_instance.send_mount_points()
                    # As long we we're hearing from an instance, assume all OK.
                    if (Time.now() - w_instance.last_comm).seconds < 22:
                        # log.debug("Instance {0} OK (heard from it {1} secs ago)".format(
                        #     w_instance.get_desc(),
                        #     (Time.now() - w_instance.last_comm).seconds))
                        continue
                    # Explicitly check the state of a quiet instance (but only
                    # periodically)
                    elif (Time.now() - w_instance.last_state_update).seconds > 30:
                        log.debug("Have not heard from or checked on instance {0} "
                            "for a while; checking now.".format(w_instance.get_desc()))
                        w_instance.maintain()
                    else:
                        log.debug("Instance {0} has been quiet for a while (last check "
                            "{1} secs ago); will wait a bit longer before a check..."
                            .format(w_instance.get_desc(),
                            (Time.now() - w_instance.last_state_update).seconds))
            self.__add_services()
            self.__check_amqp_messages()


class Instance(object):
    def __init__(self, app, inst=None, m_state=None, last_m_state_change=None,
                 sw_state=None, reboot_required=False, spot_request_id=None):
        self.app = app
        self.config = app.config
        self.spot_request_id = spot_request_id
        self.lifecycle = instance_lifecycle.SPOT if self.spot_request_id else instance_lifecycle.ONDEMAND
        self.inst = inst  # boto object of the instance
        self.spot_state = None
        self.private_ip = None
        self.public_ip = None
        self.local_hostname = None
        if inst:
            try:
                self.id = str(inst.id)
            except EC2ResponseError, e:
                log.error("Error retrieving instance id: %s" % e)
        else:
            self.id = None
        # Machine state as obtained from the cloud middleware (see
        # instance_states Bunch)
        self.m_state = m_state
        self.last_m_state_change = Time.now()
        # A time stamp when the most recent update of the instance state
        # (m_state) took place
        self.last_state_update = Time.now()
        self.sw_state = sw_state  # Software state
        self.is_alive = False
        self.node_ready = False
        self.num_cpus = 1
        self.time_rebooted = TIME_IN_PAST  # Initialize to a date in the past
        self.reboot_count = 0
        self.terminate_attempt_count = 0
        self.last_comm = TIME_IN_PAST  # Initialize to a date in the past
        self.nfs_data = 0
        self.nfs_tools = 0
        self.nfs_indices = 0
        self.nfs_sge = 0
        self.nfs_tfs = 0  # Transient file system, NFS-mounted from the master
        self.get_cert = 0
        self.sge_started = 0
        self.worker_status = 'Pending'  # Pending, Wake, Startup, Ready, Stopping, Error
        self.load = 0
        self.type = 'Unknown'
        self.reboot_required = reboot_required
        self.update_spot()

    def maintain(self):
        """ Based on the state and status of this instance, try to do the right thing
            to keep the instance functional. Note that this may lead to terminating
            the instance.
        """
        def reboot_terminate_logic():
            """ Make a decision whether to terminate or reboot an instance.
                CALL THIS METHOD CAREFULLY because it defaults to terminating the
                instance!
            """
            if self.reboot_count < self.config.instance_reboot_attempts:
                self.reboot()
            elif self.terminate_attempt_count >= self.config.instance_terminate_attempts:
                log.info("Tried terminating instance {0} {1} times but was unsuccessful. Giving up."
                    .format(self.inst.id, self.config.instance_terminate_attempts))
                self._remove_instance()
            else:
                log.info("Instance {0} not responding after {1} reboots. Terminating instance."
                    .format(self.id, self.reboot_count))
                self.terminate()

        # Update state then do resolution
        state = self.get_m_state()
        if state == instance_states.PENDING or state == instance_states.SHUTTING_DOWN:
            if (Time.now() - self.last_m_state_change).seconds > self.config.instance_state_change_wait and \
               (Time.now() - self.time_rebooted).seconds > self.config.instance_reboot_timeout:
                log.debug("'Maintaining' instance {0} stuck in '{1}' state.".format(
                    self.get_desc(), state))
                reboot_terminate_logic()
        elif state == instance_states.ERROR:
            log.debug(
                "'Maintaining' instance {0} in '{1}' state.".format(self.get_desc(),
                instance_states.ERROR))
            reboot_terminate_logic()
        elif state == instance_states.TERMINATED:
            log.debug(
                "'Maintaining' instance {0} in '{1}' state.".format(self.get_desc(),
                instance_states.TERMINATED))
            self._remove_instance()
        elif state == instance_states.RUNNING:
            log.debug("'Maintaining' instance {0} in '{1}' state (last comm before {2} | "
                "last m_state change before {3} | time_rebooted before {4}".format(
                    self.get_desc(), instance_states.RUNNING,
                    dt.timedelta(seconds=(Time.now() - self.last_comm).seconds),
                    dt.timedelta(seconds=(Time.now() - self.last_m_state_change).seconds),
                    dt.timedelta(seconds=(Time.now() - self.time_rebooted).seconds)))
            if (Time.now() - self.last_comm).seconds > self.config.instance_comm_timeout and \
               (Time.now() - self.last_m_state_change).seconds > self.config.instance_state_change_wait and \
               (Time.now() - self.time_rebooted).seconds > self.config.instance_reboot_timeout:
                reboot_terminate_logic()

    def get_cloud_instance_object(self, deep=False):
        """ Get the instance object for this instance from the library used to
            communicate with the cloud middleware. In the case of boto, this
            is the boto EC2 Instance object.

            :type deep: bool
            :param deep: If True, force the check with the cloud middleware; else
                         use local field by default

            :rtype: boto.ec2.instance.Instance (should really be a more generic repr
                    but we'll wait for OCCI or something)
            :return: cloud instance object for this instance
        """
        if self.app.TESTFLAG is True:
            log.debug(
                "Attempted to get instance cloud object, but TESTFLAG is set. Returning None")
            return None
        if deep is True:  # reset the current local instance field
            self.inst = None
        if self.inst is None and self.id is not None:
            try:
                rs = self.app.cloud_interface.get_all_instances(self.id)
                if len(rs) == 0:
                    log.warning("Instance {0} not found on the cloud?".format(
                        self.id))
                for r in rs:
                    # Update local fields
                    self.inst = r.instances[0]
                    self.id = r.instances[0].id
                    self.m_state = r.instances[0].state
            except EC2ResponseError, e:
                log.error("Trouble getting the cloud instance ({0}) object: {1}"
                    .format(self.id, e))
            except Exception, e:
                log.error("Error getting the cloud instance ({0}) object: {1}"
                    .format(self.id, e))
        elif not self.is_spot():
            log.debug(
                "Cannot get cloud instance object without an instance ID?")
        return self.inst

    def is_spot(self):
        """ Test is this Instance is a Spot instance.

            :rtype: bool
            :return: True if the current Instance is Spot instance, False otherwise.
        """
        return self.lifecycle == instance_lifecycle.SPOT

    def spot_was_filled(self):
        """ For Spot-based instances, test if the spot request has been
            filled (ie, an instance was started)

            :rtype: bool
            :return: True if this is a Spot instance and the Spot request
                     is in state spot_states.ACTIVE. False otherwise.
        """
        self.update_spot()
        if self.is_spot() and self.spot_state == spot_states.ACTIVE:
            return True
        return False

    def get_status_dict(self):
        toret = {'id': self.id,
                 'ld': self.load,
                 'time_in_state': misc.formatSeconds(Time.now() - self.last_m_state_change),
                 'nfs_data': self.nfs_data,
                 'nfs_tools': self.nfs_tools,
                 'nfs_indices': self.nfs_indices,
                 'nfs_sge': self.nfs_sge,
                 'nfs_tfs': self.nfs_tfs,
                 'get_cert': self.get_cert,
                 'sge_started': self.sge_started,
                 'worker_status': self.worker_status,
                 'instance_state': self.m_state,
                 'instance_type': self.type,
                 'public_ip': self.public_ip}

        if self.load != 0:
            lds = self.load.split(' ')
            if len(lds) == 3:
                toret['ld'] = "%s %s %s" % (float(lds[0]) / self.num_cpus, float(
                    lds[1]) / self.num_cpus, float(lds[2]) / self.num_cpus)
        return toret

    def get_status_array(self):
        if self.m_state.lower() == "running":  # For extra states.
            if self.is_alive is not True:
                ld = "Starting"
            elif self.load != 0:
                lds = self.load.split(' ')
                if len(lds) == 3:
                    try:
                        load1 = float(lds[0]) / self.num_cpus
                        load2 = float(lds[1]) / self.num_cpus
                        load3 = float(lds[2]) / self.num_cpus
                        ld = "%s %s %s" % (load1, load2, load3)
                    except Exception, e:
                        log.debug("Problems normalizing load: %s" % e)
                        ld = self.load
                else:
                    ld = self.load
            elif self.node_ready:
                ld = "Running"
            return [self.id, ld, misc.formatSeconds(
                Time.now() - self.last_m_state_change),
                self.nfs_data, self.nfs_tools, self.nfs_indices, self.nfs_sge, self.get_cert,
                self.sge_started, self.worker_status]
        else:
            return [self.id, self.m_state, misc.formatSeconds(Time.now() - self.last_m_state_change),
                self.nfs_data, self.nfs_tools, self.nfs_indices, self.nfs_sge, self.get_cert,
                self.sge_started, self.worker_status]

    def get_id(self):
        if self.app.TESTFLAG is True:
            log.debug(
                "Attempted to get instance id, but TESTFLAG is set. Returning TestInstanceID")
            return "TestInstanceID"
        if self.inst is not None and self.id is None:
            try:
                self.inst.update()
                self.id = self.inst.id
            except EC2ResponseError, e:
                log.error("Error retrieving instance id: %s" % e)
            except Exception, e:
                log.error("Exception retreiving instance object: %s" % e)
        return self.id

    def get_desc(self):
        """ Get basic but descriptive info about this instance. Useful for logging.
        """
        if self.is_spot() and not self.spot_was_filled():
            return "'{sid}'".format(sid=self.spot_request_id)
        return "'{id}' (IP: {ip})".format(id=self.get_id(), ip=self.get_public_ip())

    def reboot(self, count_reboot=True):
        """
        Reboot this instance. If ``count_reboot`` is set, increment the number
        of reboots for this instance (a treshold in this count leads to eventual
        instance termination, see ``self.config.instance_reboot_attempts``).
        """
        if self.inst is not None:
            # Show reboot count only if this reboot counts toward the reboot quota
            s = " (reboot #{0})".format(self.reboot_count + 1)
            log.info("Rebooting instance {0}{1}.".format(self.get_desc(),
                s if count_reboot else ''))
            try:
                self.inst.reboot()
                self.time_rebooted = Time.now()
            except EC2ResponseError, e:
                log.error("Trouble rebooting instance {0}: {1}".format(self.get_desc(), e))
        else:
            log.debug("Attampted to reboot instance {0} but no instance object? "
                "(doing nothing)".format(self.get_desc()))
        if count_reboot:
            # Increment irespective of success to allow for eventual termination
            self.reboot_count += 1
            log.debug("Incremented instance reboot count to {0} (out of {1})"
                .format(self.reboot_count, self.config.instance_reboot_attempts))

    def terminate(self):
        self.worker_status = "Stopping"
        t_thread = threading.Thread(target=self.__terminate)
        t_thread.start()
        return t_thread

    def __terminate(self):
        inst_terminated = self.app.cloud_interface.terminate_instance(
            instance_id=self.id,
            spot_request_id=self.spot_request_id if self.is_spot() else None)
        self.terminate_attempt_count += 1
        if inst_terminated is False:
            log.error("Terminating instance %s did not go smoothly; instance state: '%s'"
                % (self.get_desc(), self.get_m_state()))
        else:
            # Remove the reference to the instance object because with OpenStack &
            # boto the instance.update() method returns the instance as being
            # in 'running' state even though the instance does not even exist
            # any more.
            self.inst = None
            self._remove_instance()

    def _remove_instance(self, force=False):
        """ A convenience method to remove the current instance from the list
            of worker instances tracked by the master object.

            :type force: bool
            :param force: Indicate if the instance should be forcefully (ie, irrespective)
                          of other logic) removed from the list of instances maintained
                          by the master object.
        """
        try:
            if self in self.app.manager.worker_instances:
                self.app.manager.worker_instances.remove(self)
                log.info(
                    "Instance '%s' removed from the internal instance list." % self.id)
                # If this was the last worker removed, add master back as execution host.
                if len(self.app.manager.worker_instances) == 0 and not self.app.manager.master_exec_host:
                    self.app.manager.toggle_master_as_exec_host()
        except ValueError, e:
            log.warning("Instance '%s' no longer in instance list, the global monitor probably "
                "picked it up and deleted it already: %s" % (self.id, e))

    def instance_can_be_terminated(self):
        log.debug("Checking if instance '%s' can be terminated" % self.id)
        # TODO (qstat -qs {a|c|d|o|s|u|A|C|D|E|S})
        return False

    def get_m_state(self):
        """ Update the machine state of the current instance by querying the
            cloud middleware for the instance object itself (via the instance
            id) and updating self.m_state field to match the state returned by
            the cloud middleware.
            Also, update local last_state_update timestamp.

            :rtype: String
            :return: the current state of the instance as obtained from the
                     cloud middleware
        """
        if self.app.TESTFLAG is True:
            log.debug("Getting m_state for instance {0} but TESTFLAG is set; returning 'running'"
                .format(self.get_id()))
            return "running"
        self.last_state_update = Time.now()
        self.get_cloud_instance_object(deep=True)
        if self.inst:
            try:
                state = self.inst.state
                log.debug("Requested instance {0} update: old state: {1}; new state: {2}"
                    .format(self.get_desc(), self.m_state, state))
                if state != self.m_state:
                    self.m_state = state
                    self.last_m_state_change = Time.now()
            except EC2ResponseError, e:
                log.debug("Error updating instance {0} state: {1}".format(
                    self.get_id(), e))
                self.m_state = instance_states.ERROR
        else:
            if not self.is_spot() or self.spot_was_filled():
                log.debug("Instance object {0} not found during m_state update; "
                    "setting instance state to {1}".format(self.get_id(), instance_states.TERMINATED))
                self.m_state = instance_states.TERMINATED
        return self.m_state

    @TestFlag(None)
    def send_alive_request(self):
        self.app.manager.console_monitor.conn.send('ALIVE_REQUEST', self.id)

    def send_sync_etc_host(self, msg):
        # Because the hosts file is synced over the transientFS, give the FS
        # some time to become available before sending the msg
        for i in range(5):
            if int(self.nfs_tfs):
                self.app.manager.console_monitor.conn.send('SYNC_ETC_HOSTS | ' + msg, self.id)
                break
            log.debug("Transient FS on instance not available; waiting a bit...")
            time.sleep(7)

    def send_status_check(self):
        # log.debug("\tMT: Sending STATUS_CHECK message" )
        if self.app.TESTFLAG is True:
            return
        self.app.manager.console_monitor.conn.send('STATUS_CHECK', self.id)
        # log.debug( "\tMT: Message STATUS_CHECK sent; waiting on response" )

    def send_worker_restart(self):
        # log.info("\tMT: Sending restart message to worker %s" % self.id)
        if self.app.TESTFLAG is True:
            return
        self.app.manager.console_monitor.conn.send('RESTART | %s' % self.app.cloud_interface.get_private_ip(), self.id)
        log.info("\tMT: Sent RESTART message to worker '%s'" % self.id)

    def update_spot(self, force=False):
        """ Get an update on the state of a Spot request. If the request has entered
            spot_states.ACTIVE or spot_states.CANCELLED states, update the Instance
            object itself otherwise just update state. The method will continue to poll
            for an update until the spot request has been filled (ie, enters state
            spot_states.ACTIVE). After that, simply return the spot state (see
            force parameter).

            :type force: bool
            :param force: If True, poll for an update on the spot request,
                          irrespective of the stored spot request state.
        """
        if self.is_spot() and (force or self.spot_state != spot_states.ACTIVE):
            old_state = self.spot_state
            try:
                ec2_conn = self.app.cloud_interface.get_ec2_connection()
                reqs = ec2_conn.get_all_spot_instance_requests(
                    request_ids=[self.spot_request_id])
                for req in reqs:
                    self.spot_state = req.state
                    # Also update the worker_status because otherwise there's no
                    # single source to distinguish between simply an instance
                    # in Pending state and a Spot request
                    self.worker_status = self.spot_state
                    # If the state has changed, do a deeper update
                    if self.spot_state != old_state:
                        if self.spot_state == spot_states.CANCELLED:
                            # The request was canceled so remove this Instance
                            # object
                            log.info("Spot request {0} was canceled; removing Instance object {1}"
                                .format(self.spot_request_id, self.id))
                            self._remove_instance()
                        elif self.spot_state == spot_states.ACTIVE:
                            # We should have an instance now
                            self.id = req.instance_id
                            log.info("Spot request {0} filled with instance {1}"
                                .format(self.spot_request_id, self.id))
                            # Potentially give it a few seconds so everything gets registered
                            for i in range(3):
                                instance = self.get_cloud_instance_object()
                                if instance:
                                    self.app.cloud_interface.add_tag(instance,
                                        'clusterName', self.app.ud['cluster_name'])
                                    self.app.cloud_interface.add_tag(instance, 'role', 'worker')
                                    self.app.cloud_interface.add_tag(instance,
                                        'Name', "Worker: {0}".format(self.app.ud['cluster_name']))
                                    break
                                time.sleep(5)
            except EC2ResponseError, e:
                log.error("Trouble retrieving spot request {0}: {1}".format(
                    self.spot_request_id, e))
        return self.spot_state

    def get_private_ip(self):
        # log.debug("Getting instance '%s' private IP: '%s'" % ( self.id, self.private_ip ) )
        if self.app.TESTFLAG is True:
            log.debug(
                "Attempted to get instance private IP, but TESTFLAG is set. Returning 127.0.0.1")
            self.private_ip = '127.0.0.1'
        if self.private_ip is None:
            inst = self.get_cloud_instance_object()
            if inst is not None:
                try:
                    inst.update()
                    self.private_ip = inst.private_ip_address
                except EC2ResponseError:
                    log.debug("private_ip_address for instance {0} not (yet?) available."
                        .format(self.get_id()))
            else:
                log.debug("private_ip_address for instance {0} with no instance object not available."
                    .format(self.get_id()))
        return self.private_ip

    @TestFlag('127.0.0.1')
    def get_public_ip(self):
        """
        Get the public IP address of this worker instance.
        """
        if not self.public_ip:
            inst = self.get_cloud_instance_object(deep=True)
            # log.debug('Getting public IP for instance {0}'.format(inst.id))
            if inst:
                try:
                    inst.update()
                    self.public_ip = inst.ip_address
                    if self.public_ip:
                        log.debug("Got public IP for instance {0}: {1}".format(
                            self.get_id(), self.public_ip))
                    else:
                        log.debug("Still no public IP for instance {0}".format(
                            self.get_id()))
                except EC2ResponseError:
                    log.debug("ip_address for instance {0} not (yet?) available.".format(
                        self.get_id()))
            else:
                log.debug("ip_address for instance {0} with no instance object not available."
                    .format(self.get_id()))
        return self.public_ip

    def get_local_hostname(self):
        return self.local_hostname

    def send_mount_points(self):
        mount_points = []
        for fs in self.app.manager.get_services(svc_type=ServiceType.FILE_SYSTEM):
            if fs.nfs_fs:
                fs_type = "nfs"
                server = fs.nfs_fs.device
                options = fs.nfs_fs.mount_options
            elif fs.gluster_fs:
                fs_type = "glusterfs"
                server = fs.gluster_fs.device
                options = fs.gluster_fs.mount_options
            else:
                fs_type = "nfs"
                server = self.app.cloud_interface.get_private_ip()
                options = None
            mount_points.append(
                {'fs_type': fs_type,
                 'server': server,
                 'mount_options': options,
                 'shared_mount_path': fs.get_details()['mount_point'],
                 'fs_name': fs.get_details()['name']})
        jmp = json.dumps({'mount_points': mount_points})
        self.app.manager.console_monitor.conn.send('MOUNT | %s' % jmp, self.id)
        # log.debug("Sent mount points %s to worker %s" % (mount_points, self.id))

    def send_master_pubkey(self):
        # log.info("\tMT: Sending MASTER_PUBKEY message: %s" % self.app.manager.get_root_public_key() )
        self.app.manager.console_monitor.conn.send('MASTER_PUBKEY | %s'
            % self.app.manager.get_root_public_key(), self.id)
        log.debug("Sent master public key to worker instance '%s'." % self.id)
        log.debug("\tMT: Message MASTER_PUBKEY %s sent to '%s'"
            % (self.app.manager.get_root_public_key(), self.id))

    def send_start_sge(self):
        log.debug("\tMT: Sending START_SGE message to instance '%s'" % self.id)
        self.app.manager.console_monitor.conn.send('START_SGE', self.id)

    def send_add_s3fs(self, bucket_name, svc_roles):
        msg = 'ADDS3FS | {0} | {1}'.format(bucket_name, ServiceRole.to_string(svc_roles))
        self._send_msg(msg)

    # def send_add_nfs_fs(self, nfs_server, fs_name, svc_roles, username=None, pwd=None):
    #     """
    #     Send a message to the worker node requesting it to mount a new file system
    #     form the ``nfs_server`` at mount point /mnt/``fs_name`` with roles``svc_roles``.
    #     """
    #     nfs_server_info = {
    #         'nfs_server': nfs_server, 'fs_name': fs_name, 'username': username,
    #         'pwd': pwd, 'svc_roles': ServiceRole.to_string(svc_roles)
    #     }
    #     msg = "ADD_NFS_FS | {0}".format(json.dumps({'nfs_server_info': nfs_server_info}))
    #     self._send_msg(msg)

    def _send_msg(self, msg):
        """
        An internal convenience method to log and send a message to the current instance.
        """
        log.debug("\tMT: Sending message '{msg}' to instance {inst}".format(msg=msg, inst=self.id))
        self.app.manager.console_monitor.conn.send(msg, self.id)

    def handle_message(self, msg):
        # log.debug( "Handling message: %s from %s" % ( msg, self.id ) )
        self.is_alive = True
        self.last_comm = Time.now()
        # Transition from states to a particular response.
        if self.app.manager.console_monitor.conn:
            msg_type = msg.split(' | ')[0]
            if msg_type == "ALIVE":
                self.worker_status = "Starting"
                log.info("Instance %s reported alive" % self.get_desc())
                msp = msg.split(' | ')
                self.private_ip = msp[1]
                self.public_ip = msp[2]
                self.zone = msp[3]
                self.type = msp[4]
                self.ami = msp[5]
                try:
                    self.local_hostname = msp[6]
                except:
                    # Older versions of CloudMan did not pass this value so if the master
                    # and the worker are running 2 diff versions (can happen after an
                    # automatic update), don't crash here.
                    self.local_hostname = self.public_ip
                log.debug("INSTANCE_ALIVE private_dns:%s public_dns:%s pone:%s type:%s ami:%s hostname: %s"
                    % (self.private_ip,
                       self.public_ip,
                       self.zone,
                       self.type,
                       self.ami,
                       self.local_hostname))
                # Instance is alive and functional. Send master pubkey.
                self.send_mount_points()
            elif msg_type == "GET_MOUNTPOINTS":
                self.send_mount_points()
            elif msg_type == "MOUNT_DONE":
                self.send_master_pubkey()
                # Add hostname to /etc/hosts (for SGE config)
                if self.app.cloud_type in ('openstack', 'eucalyptus'):
                    hn2 = ''
                    if '.' in self.local_hostname:
                        hn2 = (self.local_hostname).split('.')[0]
                    worker_host_line = '{ip} {hn1} {hn2}\n'.format(ip=self.private_ip,
                        hn1=self.local_hostname, hn2=hn2)

                    log.debug("worker_host_line: {0}".format(worker_host_line))
                    with open('/etc/hosts', 'r+') as f:
                        hosts = f.readlines()
                        if worker_host_line not in hosts:
                            log.debug("Adding worker {0} to /etc/hosts".format(
                                self.local_hostname))
                            f.write(worker_host_line)

                if self.app.cloud_type == 'opennebula':
                    f = open("/etc/hosts", 'a')
                    f.write("%s\tworker-%s\n" % (self.private_ip, self.id))
                    f.close()
            elif msg_type == "WORKER_H_CERT":
                self.is_alive = True  # This is for the case that an existing worker is added to a new master.
                self.app.manager.save_host_cert(msg.split(" | ")[1])
                log.debug("Worker '%s' host certificate received and appended to /root/.ssh/known_hosts"
                    % self.id)
                try:
                    sge_svc = self.app.manager.get_services(
                        svc_role=ServiceRole.SGE)[0]
                    if sge_svc.add_sge_host(self.get_id(), self.local_hostname):
                        # Send a message to worker to start SGE
                        self.send_start_sge()
                        # If there are any bucket-based FSs, tell the worker to
                        # add those
                        fss = self.app.manager.get_services(
                            svc_type=ServiceType.FILE_SYSTEM)
                        for fs in fss:
                            if len(fs.buckets) > 0:
                                for b in fs.buckets:
                                    self.send_add_s3fs(b.bucket_name, fs.svc_roles)
                        log.info("Waiting on worker instance %s to configure itself..."
                            % self.get_desc())
                    else:
                        log.error("Adding host to SGE did not go smoothly, "
                            "not instructing worker to configure SGE daemon.")
                except IndexError:
                    log.error(
                        "Could not get a handle on SGE service to add a host; host not added")
            elif msg_type == "NODE_READY":
                self.node_ready = True
                self.worker_status = "Ready"
                log.info("Instance %s ready" % self.get_desc())
                msplit = msg.split(' | ')
                try:
                    self.num_cpus = int(msplit[2])
                except:
                    log.debug(
                        "Instance '%s' num CPUs is not int? '%s'" % (self.id, msplit[2]))
                log.debug("Instance '%s' reported as having '%s' CPUs." %
                          (self.id, self.num_cpus))
                # Make sure the instace is tagged (this is also necessary to do
                # here for OpenStack because it does not allow tags to be added
                # until an instance is 'running')
                self.app.cloud_interface.add_tag(self.inst, 'clusterName', self.app.ud['cluster_name'])
                self.app.cloud_interface.add_tag(self.inst, 'role', 'worker')
                self.app.cloud_interface.add_tag(self.inst, 'Name', "Worker: {0}"
                    .format(self.app.ud['cluster_name']))

                log.debug("update condor host through master")
                self.app.manager.update_condor_host(self.public_ip)
                log.debug("update etc host through master")
                self.app.manager.update_etc_host()
            elif msg_type == "NODE_STATUS":
                msplit = msg.split(' | ')
                self.nfs_data = msplit[1]
                self.nfs_tools = msplit[2]  # Workers currently do not update this field
                self.nfs_indices = msplit[3]
                self.nfs_sge = msplit[4]
                self.get_cert = msplit[5]
                self.sge_started = msplit[6]
                self.load = msplit[7]
                self.worker_status = msplit[8]
                self.nfs_tfs = msplit[9]
            elif msg_type == 'NODE_SHUTTING_DOWN':
                msplit = msg.split(' | ')
                self.worker_status = msplit[1]
            else:  # Catch-all condition
                log.debug("Unknown Message: %s" % msg)
        else:
            log.error("Epic Failure, squeue not available?")
