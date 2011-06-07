import urllib, socket
from cm.clouds import CloudInterface

from boto.s3.connection import S3Connection
from boto.ec2.connection import EC2Connection
from boto.exception import EC2ResponseError

import logging
log = logging.getLogger( 'cloudman' )


class EC2Interface(CloudInterface):
    
    def __init__(self, aws_access_key, aws_secret_key, app=None):
        super(EC2Interface, self).__init__()
        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.app = app
    
    def get_ami( self ):
        if self.ami is None:
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance ami, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/ami-id')
                    self.ami = fp.read()
                    fp.close()
                    if self.ami:
                        break
                except IOError:
                    pass
        return self.ami
    
    def get_type( self ):
        if self.instance_type is None:
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance type, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/instance-type')
                    self.instance_type = fp.read()
                    fp.close()
                    if self.instance_type:
                        break
                except IOError:
                    pass
        return self.instance_type
    
    def get_instance_id( self ):
        if self.instance_id is None:
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance id, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/instance-id')
                    self.instance_id = fp.read()
                    fp.close()
                    if self.instance_id:
                        log.debug("Instance ID is '%s'" % self.instance_id)
                        break
                except IOError:
                    pass
        return self.instance_id
    
    def get_zone( self ):
        if self.zone is None:
            if self.app.TESTFLAG is True:
                log.debug("Attempted to get instance zone, but TESTFLAG is set. Returning 'us-east-1a'")
                self.zone = 'us-east-1a'
                return self.zone
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance zone, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/placement/availability-zone')
                    self.zone = fp.read()
                    fp.close()
                    if self.zone:
                        log.debug("Instance zone is '%s'" % self.zone)
                        break
                except IOError:
                    pass
        return self.zone
    
    def get_security_groups( self ):
        if self.security_groups is None:
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance security group, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/security-groups')
                    self.security_groups = []
                    for line in fp.readlines():
                        self.security_groups.append(line.strip())
                    fp.close()
                    if self.security_groups:
                        break
                except IOError:
                    pass
        return self.security_groups
    
    def get_key_pair_name( self ):
        if self.key_pair_name is None:
            if self.app.TESTFLAG is True:
                log.debug("Attempted to get key pair name, but TESTFLAG is set. Returning 'local_keypair'")
                self.key_pair_name = 'local_keypair'
                return self.key_pair_name
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance public keys (i.e., key pairs), attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/public-keys')
                    public_keys = fp.read()
                    self.key_pair_name = public_keys.split('=')[1]
                    fp.close()
                    if self.key_pair_name:
                        log.debug( "Got key pair: '%s'" % self.key_pair_name )
                        break
                except IOError:
                    pass
        return self.key_pair_name
    
    def get_self_private_ip( self ):
        if self.self_private_ip is None:
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance private hostname, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/local-hostname')
                    self.self_private_ip = fp.read()
                    fp.close()
                    if self.self_private_ip:
                        break
                except IOError:
                    pass
        return self.self_private_ip
    
    def get_self_public_ip( self ):
        if self.self_public_ip is None:
            if self.app.TESTFLAG is True:
                log.debug("Attempted to get public IP, but TESTFLAG is set. Returning '127.0.0.1'")
                self.self_public_ip = '127.0.0.1'
                return self.self_public_ip
            for i in range(0, 5):
                try:
                    log.debug('Gathering instance public hostname, attempt %s' % i)
                    fp = urllib.urlopen('http://169.254.169.254/latest/meta-data/public-hostname')
                    self.self_public_ip = fp.read()
                    fp.close()
                    if self.self_public_ip:
                        break
                except Exception, e:
                    log.error ( "Error retrieving FQDN: %s" % e )
                                
        return self.self_public_ip
    
    def get_fqdn(self):
        log.debug( "Retrieving FQDN" )
        if self.fqdn == None:
            try:
                self.fqdn = socket.getfqdn()
            except IOError:
                pass
        return self.fqdn
    
    def get_ec2_connection( self ):
        if self.ec2_conn == None:
            try:
                log.debug( 'Establishing boto EC2 connection' )
                if self.app.TESTFLAG is True:
                    log.debug("Attempted to establish EC2 connection, but TESTFLAG is set. Returning default EC2 connection.")
                    self.ec2_conn = EC2Connection(self.aws_access_key, self.aws_secret_key)
                    return self.ec2_conn
                # In order to get a connection for the correct region, get instance zone and go from there
                zone = self.get_zone()[:-1] # truncate zone and be left with region name
                tmp_conn = EC2Connection(self.aws_access_key, self.aws_secret_key) # get conn in default region
                try:
                    regions = tmp_conn.get_all_regions()
                except EC2ResponseError, e:
                    log.error("Cannot validate provided AWS credentials: %s" % e)
                # Find region that matches instance zone and then create ec2_conn
                for r in regions:
                    if zone in r.name:
                        region = r
                        break
                self.ec2_conn = EC2Connection(self.aws_access_key, self.aws_secret_key, region=region)
                # Do a simple query to test if provided credentials are valid
                try:
                    self.ec2_conn.get_all_instances()
                    log.debug("Got boto EC2 connection for region '%s'" % self.ec2_conn.region.name)
                except EC2ResponseError, e:
                    log.error("Cannot validate provided AWS credentials (A:%s, S:%s): %s" % (self.aws_access_key, self.aws_secret_key, e))
                    self.ec2_conn = False
            except Exception, e:
                log.error(e)
        return self.ec2_conn
    
    def get_s3_connection( self ):
        log.debug( 'Getting boto S3 connection' )
        if self.s3_conn == None:
            log.debug("No S3 Connection, creating a new one.")
            try:
                self.s3_conn = S3Connection(self.aws_access_key, self.aws_secret_key)
                log.debug( 'Got boto S3 connection.' )
                # try:
                #     self.s3_conn.get_bucket('test_creds') # Any bucket name will do - just testing the call
                #     log.debug( 'Got boto S3 connection.' )
                # except S3ResponseError, e:
                #     log.error("Cannot validate provided AWS credentials: %s" % e)
                #     self.s3_conn = False
            except Exception, e:
                log.error(e)
        return self.s3_conn
    
