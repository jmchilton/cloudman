import os
from cm.services import ServiceRole
from cm.services import service_states
from cm.services import ServiceDependency
from cm.services import ServiceType
from cm.services.apps import ApplicationService
from cm.util import misc, paths

import logging
log = logging.getLogger('cloudman')

class Migrate1to2:
    """Functionality for upgrading from version 1 to 2.
    """
    def _as_postgres(self, cmd):
        return misc.run('%s - postgres -c "%s"' % (paths.P_SU, cmd))

    def _upgrade_postgres_8_to_9(self, app):
        data_dir = os.path.join(app.path_resolver.galaxy_data, "pgsql", "data")
        if os.path.exists(data_dir):
            with open(os.path.join(data_dir, "PG_VERSION")) as in_handle:
                version = in_handle.read().strip()
            if version.startswith("8"):
                log.info("Upgrading Postgres from version 8 to 9")
                old_debs = ["http://launchpadlibrarian.net/102366400/postgresql-client-8.4_8.4.11-1_amd64.deb",
                            "http://launchpadlibrarian.net/102366396/postgresql-8.4_8.4.11-1_amd64.deb"]
                for deb in old_debs:
                    misc.run("wget %s" % deb)
                    misc.run("dpkg -i %s" % os.path.basename(deb))
                    misc.run("rm -f %s" % os.path.basename(deb))
                backup_dir = os.path.join(app.path_resolver.galaxy_data, "pgsql",
                                          "data-backup-v%s" % version)
                self._as_postgres("mv %s %s" % (data_dir, backup_dir))
                self._as_postgres("%s/initdb %s" % (app.path_resolver.pg_home, data_dir))
                self._as_postgres("pg_createcluster -d %s %s old_galaxy" % (backup_dir, version))
                self._as_postgres("pg_upgradecluster %s old_galaxy %s" % (version, data_dir))
                misc.run("apt-get remove postgresql-8.4 postgresql-client-8.4")

    def _move_postgres_location(self, app):
        old_dir = os.path.join(app.path_resolver.galaxy_data, "pgsql")
        if os.path.exists(old_dir) and not os.path.exists(app.path_resolver.psql_dir):
            log.info("Moving Postgres location from %s to %s" %
                     (old_dir, app.path_resolver.psql_dir))
            misc.run("mv %s %s" % (old_dir, app.path_resolver.psql_dir))

class MigrationService(ApplicationService, Migrate1to2):
    def __init__(self, app):
        super(MigrationService, self).__init__(app)
        self.svc_roles = [ServiceRole.MIGRATION]
        self.name = ServiceRole.to_string(ServiceRole.MIGRATION)
        # Wait for galaxy data & indices to come up before attempting migration
        self.reqs = [ServiceDependency(self, ServiceRole.GALAXY_DATA),
                     ServiceDependency(self, ServiceRole.GALAXY_INDICES)]

    def start(self):
        """
        Start the migration service
        """
        log.debug("Starting migration service...")
        self.state = service_states.STARTING
        self._start()
        self.state = service_states.RUNNING

    def _start(self):
        """
        Do the actual work
        """
        if self._is_migration_needed():
            log.debug("Migration is required. Starting...")
            self._perform_migration()

    def _perform_migration(self):
        """
        Based on the version number, carry out appropriate migration actions
        """
        if self._get_old_cm_version() <= 1:
            self.migrate_1()

    def _is_migration_needed(self):
        return self._get_old_cm_version() < self._get_new_cm_version()

    def _get_new_cm_version(self):
        return 2  # Whichever version that this upgrade script last understands

    def _get_old_cm_version(self):
        # TODO: Need old version discovery. Where do we get that from?
        version = self.app.ud.get('cloudman_version', None)
        if version is None:
            version = 1  # A version prior to version number being introduced

    def migrate_1(self):
        log.debug("Migrating from version 1 to 2...")
        self._upgrade_postgres_8_to_9(self.app)
        self._move_postgres_location(self.app)
        # copy tools FS to the data FS
        # adjust directory names/paths to match the new FS structure
        # sed for predefined full old paths (eg, Galaxy's env.sh files, EMBOSS tools?)
        # create new directory structure with any missing dirs
        # unmount file systems from persistent_data.yaml
        # update persistent_data.yaml

        # Finally - shutdown all filesystem services
        log.debug("Migration: Shutting down all file system services...")
        fs_svcs = self.app.manager.get_services(svc_type=ServiceType.FILE_SYSTEM)
        # TODO: Is a clean necessary?
        for svc in fs_svcs:
            svc.remove()

        log.debug("Migration: Restarting all file system services...")
        # Restart file system services
        self.app.manager.add_preconfigured_services()

    def remove(self):
        """
        Remove the migration service
        """
        log.info("Removing Migration service")
        self.state = service_states.SHUTTING_DOWN
        self._clean()
        self.state = service_states.SHUT_DOWN

    def _clean(self):
        """
        Clean up the system
        """
        pass

    def status(self):
        """
        Check and update the status of service
        """
        pass
