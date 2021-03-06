import os
import threading
from cm.util import misc
from cm.util import cluster_status
from cm.services import service_states
from cm.services import ServiceRole
from cm.services.apps import ApplicationService

import logging
log = logging.getLogger('cloudman')


class PSSService(ApplicationService):
    """ post_start_script service - this service runs once at the end of the
        configuration of all services defined in CloudMan. It runs a predefined
        script.
        Defining a service for something simple like this may be an overkill
        but it's also the simplest way to ensure this runs only after all other
        servces have been configured and are running. Plus, it can eventually
        be extended to run arbitrary script when a condition is met."""

    def __init__(self, app, instance_role='master'):
        super(PSSService, self).__init__(app)
        self.svc_roles = [ServiceRole.PSS]
        self.name = ServiceRole.to_string(ServiceRole.PSS)
        self.instance_role = instance_role
        log.debug("Configured PSS as {0}".format(self.instance_role))
        # Name of the default script to run
        self.pss_filename = 'post_start_script' if self.instance_role == 'master' \
            else 'worker_post_start_script'
        self.pss_url = self.app.ud.get('post_start_script_url', None) if self.instance_role == 'master' \
            else self.app.ud.get('worker_post_start_script_url', None)

    def _prime_data(self):
        """ Some data is slow to obtain because a call to the cloud middleware
            is required. When such data is required to complete a user request,
            the request may be slow to complete. In an effort to alleviate some
            of those delays, prime the data into local variables (particularly
            the ones that do not change over a lifetime of a cluster).
        """
        log.debug("Priming local data variables...")
        self.app.cloud_interface.get_ami()
        self.app.cloud_interface.get_zone()
        self.app.cloud_interface.get_key_pair_name()
        self.app.cloud_interface.get_security_groups()
        self.app.cloud_interface.get_private_ip()
        self.app.cloud_interface.get_public_ip()
        self.app.cloud_interface.get_local_hostname()

    def add(self):
        """
        Check if prerequisites for running this service are satisfied and, if so,
        start the service and return ``True``, else set its state to ``UNSTARTED``
        and return ``False``.
        """
        if (self.state != service_states.COMPLETED and
           self.app.manager.initial_cluster_type is not None):
            self.state = service_states.STARTING
            prereqs_ok = True
            # There is a race condition w/ this service so when we're setting up
            # a 'Galaxy' clutser, make sure Galaxy service actually exists before
            # deciding all the services are running.
            # TODO: there's probably a better way to do this
            awaiting_galaxy = False
            if self.app.manager.initial_cluster_type == 'Galaxy':
                awaiting_galaxy = True
            # If there is a service other than self that is not running, return.
            # Otherwise, start this service.
            for srvc in self.app.manager.services:
                if srvc != self and not (srvc.running() or srvc.completed()):
                    prereqs_ok = False
                    break
            if prereqs_ok and awaiting_galaxy:
                # Make sure Galaxy service exists before assuming all services
                # are there
                galaxy_svc = self.app.manager.get_services(svc_role=ServiceRole.GALAXY)
                if not galaxy_svc:
                    log.debug("No Galaxy service in a Galaxy cluster; waiting.")
                    prereqs_ok = False
                elif len(galaxy_svc) > 0 and not galaxy_svc[0].running():
                    log.debug("Galaxy service not running yet; waiting.")
                    prereqs_ok = False
                else:
                    log.debug("Galaxy service OK for PSS")
            if not prereqs_ok:
                log.debug("%s not running (%s), %s service prerequisites not met afterall,"
                          "not starting the service yet" % (srvc.get_full_name(), srvc.state, self.name))
                self.state = service_states.UNSTARTED  # Reset state so it gets picked up by monitor again
                return False
            self.start()
            return True
        else:
            log.debug("Not adding {0} svc; it completed ({1}) or the cluster was "
                "not yet initialized ({2})".format(self.name,
                self.state == service_states.COMPLETED, self.app.manager.initial_cluster_type))
            return False

    def start(self):
        """ Wait until all other services are running before starting this one."""
        log.debug("Starting %s service" % self.name)
        # All other services OK, start this one now
        self.state = service_states.RUNNING
        log.debug("%s service prerequisites OK (i.e., all other services running), "
                  "checking if %s was provided..." % (self.name, self.pss_filename))
        local_pss_file = os.path.join(
            self.app.ud['cloudman_home'], self.pss_filename)
        # Check user data first to allow overwriting of a potentially existing
        # script
        if self.pss_url:
            # This assumes the provided URL is readable to anyone w/o authentication
            # First check if the file actually exists
            if misc.run('wget --server-response %s' % self.pss_url):
                misc.run('wget --output-document=%s %s' % (
                    local_pss_file, self.pss_url))
            else:
                log.error(
                    "Specified post_start_script url (%s) does not exist" % self.pss_url)
        else:
            s3_conn = self.app.cloud_interface.get_s3_connection()
            b = None
            if s3_conn and 'bucket_cluster' in self.app.ud:
                b = s3_conn.lookup(self.app.ud['bucket_cluster'])
            if b is not None:  # Check if an existing cluster has a stored post start script
                log.debug("Cluster bucket '%s' found; looking for post start script '%s'"
                          % (b.name, self.pss_filename))
                misc.get_file_from_bucket(
                    s3_conn, b.name, self.pss_filename, local_pss_file)
        if os.path.exists(local_pss_file) and os.path.getsize(local_pss_file) > 0:
            log.info("%s found and saved to '%s'; running it now (note that this may take a while)"
                     % (self.pss_filename, os.path.join(self.app.ud['cloudman_home'], self.pss_filename)))
            os.chmod(local_pss_file, 0755)  # Ensure the script is executable
            misc.run('cd %s;./%s' % (self.app.ud[
                     'cloudman_home'], self.pss_filename))
            self.save_to_bucket()
            log.info("Done running {0}".format(self.pss_filename))
        else:
            log.debug("%s does not exist or could not be downloaded; continuing without running it."
                      % self.name)
        # Prime the object with instance data (because this may take a while
        # on some clouds, do so in a separate thread)
        threading.Thread(target=self._prime_data).start()
        self.state = service_states.SHUT_DOWN
        log.debug("%s service done and marked as '%s'" % (self.name, self.state))
        if self.instance_role == 'master':
            # On master, remove the service upon completion (PSS runs only
            # once)
            self.remove()
        self.state = service_states.COMPLETED
        # Once this service is complete, it's safe to assume the cluster is
        # READY
        self.app.manager.cluster_status = cluster_status.READY
        msg = "All cluster services started; the cluster is ready for use."
        log.info(msg)
        self.app.msgs.info(msg)

    def save_to_bucket(self):
        """ Save the current post start script file to the cluster's
            bucket. Do so only if the file does not already exist there
            and it not older than the local one.
        """
        s3_conn = self.app.cloud_interface.get_s3_connection()
        if not s3_conn:
            return
        pss_file = os.path.join(
            self.app.ud['cloudman_home'], self.pss_filename)
        if misc.file_in_bucket_older_than_local(s3_conn,
                                                self.app.ud['bucket_cluster'],
                                                self.pss_filename,
                                                pss_file):
            if os.path.exists(pss_file):
                log.debug("Saving current instance post start script (%s) to cluster bucket '%s' as '%s'"
                          % (pss_file, self.app.ud['bucket_cluster'], self.pss_filename))
                misc.save_file_to_bucket(
                    s3_conn, self.app.ud['bucket_cluster'], self.pss_filename, pss_file)
            else:
                log.debug("No instance post start script (%s)" % pss_file)
        else:
            log.debug("A current post start script {0} already exists in bucket {1}; not updating it"
                      .format(self.pss_filename, self.app.ud['bucket_cluster']))

    def remove(self, synchronous=False):
        super(PSSService, self).remove(synchronous)
        if self.state == service_states.UNSTARTED:
            self.state = service_states.SHUT_DOWN
        if self.state == service_states.SHUT_DOWN:
            log.debug(
                "Removing %s service from master list of services" % self.name)
            self.app.manager.remove_master_service(self)
        else:
            log.debug("Tried removing %s service but it's not in state %s"
                      % (self.name, service_states.SHUT_DOWN))

    def status(self):
        pass
