import urllib

from cm.clouds.ec2 import EC2Interface

import boto
from boto.s3.connection import OrdinaryCallingFormat
from boto.ec2.regioninfo import RegionInfo
from boto.exception import EC2ResponseError

import logging
log = logging.getLogger('cloudman')


class OSInterface(EC2Interface):

    def __init__(self, app=None):
        super(OSInterface, self).__init__()
        self.app = app
        self.tags_supported = False  # Not used here, see _tags_supported method instead
        self.set_configuration()

    def _tags_supported(self, resource):
        """
        ``True`` if tagging for a given resource is operational. ``False``
        otherwise.
        """
        # Havana release of Open Stack supports tags for Instance objects only
        if type(resource) == boto.ec2.instance.Instance:
            return True
        log.debug("Not adding tag to resource %s because that resource "
            "in OpenStack does not support tags.")
        return False

    def set_configuration(self):
        super(OSInterface, self).set_configuration()
        # Cloud details gotten from the user data
        self.region_name = self.user_data.get('region_name', self.region_name)
        self.region_endpoint = self.user_data.get('region_endpoint', None)
        self.ec2_port = self.user_data.get('ec2_port', None)
        self.ec2_conn_path = self.user_data.get('ec2_conn_path', None)
        self.is_secure = self.user_data.get('is_secure', False)
        self.s3_host = self.user_data.get('s3_host', None)
        self.s3_port = self.user_data.get('s3_port', None)
        self.s3_conn_path = self.user_data.get('s3_conn_path', None)
        self.use_private_ip = self.user_data.get('openstack_use_private_ip', False)
        self.calling_format = OrdinaryCallingFormat()

    def get_region_name(self):
        return self.region_name

    def get_ec2_connection(self):
        if not self.ec2_conn:
            try:
                if self.app.TESTFLAG is True:
                    log.debug("Attempted to establish Nova connection, but TESTFLAG is set. "
                              "Returning a default connection.")
                    self.ec2_conn = self._get_default_ec2_conn()
                    return self.ec2_conn
                log.debug('Establishing a boto Nova connection')
                self.ec2_conn = self._get_default_ec2_conn()
                # Do a simple query to test if provided credentials are valid
                try:
                    log.debug("Testing the new boto Nova connection ({0})"
                        .format(self.ec2_conn))
                    self.ec2_conn.get_all_key_pairs()
                    log.debug("Got boto Nova connection for region {0}".format(
                        self.ec2_conn.region.name))
                except EC2ResponseError, e:
                    log.error("Cannot validate provided OpenStack credentials or configuration "
                              "(AK:{0}, SK:{1}): {2}".format(self.aws_access_key, self.aws_secret_key, e))
                    self.ec2_conn = None
            except Exception, e:
                log.error("Trouble getting boto Nova connection: {0}".format(e))
                self.ec2_conn = None
        return self.ec2_conn

    def _get_default_ec2_conn(self, region=None):
        ec2_conn = None
        try:
            if region is None:
                region = RegionInfo(
                    name=self.region_name, endpoint=self.region_endpoint)
            ec2_conn = boto.connect_ec2(aws_access_key_id=self.aws_access_key,
                                        aws_secret_access_key=self.aws_secret_key,
                                        is_secure=self.is_secure,
                                        region=region,
                                        port=self.ec2_port,
                                        path=self.ec2_conn_path,
                                        validate_certs=False)  # github.com/boto/boto/wiki/2.6.0-release-notes
        except EC2ResponseError, e:
            log.error("Trouble creating a Nova connection: {0}".format(e))
        return ec2_conn

    def get_s3_connection(self):
        # TODO: Port this use_object_store logic to other clouds as well
        if self.app and not self.app.use_object_store:
            return None
        if not self.s3_conn:
            log.debug("Establishing a boto Swift connection.")
            try:
                self.s3_conn = boto.connect_s3(
                    aws_access_key_id=self.aws_access_key,
                    aws_secret_access_key=self.aws_secret_key,
                    is_secure=self.is_secure,
                    host=self.s3_host,
                    port=self.s3_port,
                    path=self.s3_conn_path,
                    calling_format=self.calling_format)
                log.debug('Got boto Swift connection.')
                # try:
                #     self.s3_conn.get_bucket('cloudman') # Any bucket name will do - just testing the call
                #     log.debug('Got boto Swift connection.')
                # except S3ResponseError, e:
                #     log.error("Cannot validate provided OpenStack credentials: %s" % e)
                #     self.s3_conn = None
            except Exception, e:
                log.error("Trouble creating a Swift connection: {0}".format(e))
        return self.s3_conn

    def get_public_ip(self):
        """ NeCTAR's public & private IPs are the same and also local-ipv4 metadata filed
            returns empty so do some monkey patching.
        """
        if self.self_public_ip is None:
            if self.app.TESTFLAG is True:
                log.debug("Attempted to get public IP, but TESTFLAG is set. Returning '127.0.0.1'")
                self.self_public_ip = '127.0.0.1'
                return self.self_public_ip
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance public IP, attempt %s' % i)
                    #This is not only nectar specific but I left nectar for backward compatibility
                    if self.use_private_ip or self.app.ud.get('cloud_name', 'ec2').lower() == 'nectar':
                        self.self_public_ip = self.get_private_ip()
                    else:
                        fp = urllib.urlopen(
                            'http://169.254.169.254/latest/meta-data/local-ipv4')
                        self.self_public_ip = fp.read()
                        fp.close()
                    if self.self_public_ip:
                        break
                except Exception, e:
                    log.error("Error retrieving FQDN: %s" % e)

        return self.self_public_ip

    def add_tag(self, resource, key, value):
        if self._tags_supported(resource):
            try:
                log.debug("Adding tag '%s:%s' to resource '%s'" % (
                    key, value, resource.id if resource.id else resource))
                resource.add_tag(key, value)
            except EC2ResponseError, e:
                log.error("Exception adding tag '%s:%s' to resource '%s': %s"
                    % (key, value, resource, e))

    def get_tag(self, resource, key):
        value = None
        if self._tags_supported(resource):
            try:
                log.debug("Getting tag '%s' on resource '%s'" % (key, resource.id))
                value = resource.tags.get(key, None)
            except EC2ResponseError, e:
                log.error("Exception getting tag '%s' on resource '%s': %s" %
                    (key, resource, e))
        return value
