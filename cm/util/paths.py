import os
import commands
from cm.util import misc
from cm.services import ServiceRole

# Commands
P_MKDIR = "/bin/mkdir"
P_RM = "/bin/rm"
P_CHOWN = "/bin/chown"
P_SU = "/bin/su"
P_MV = "/bin/mv"
P_LN = "/bin/ln"

# Configs
C_PSQL_PORT = "5840"
USER_DATA_FILE = "userData.yaml"
LOGIN_SHELL_SCRIPT = "/etc/profile"

# Paths
P_BASE_INSTALL_DIR = '/opt/galaxy/pkg'
P_SGE_ROOT = "/opt/sge"
P_SGE_TARS = "/opt/galaxy/pkg/ge6.2u5"
P_SGE_CELL = "/opt/sge/default/spool/qmaster"
P_PSQL_DIR = "/mnt/galaxyData/pgsql/data"

try:
    # Get only the first 3 chars of the version since that's all that's used for dir name
    pg_ver = load = (commands.getoutput("dpkg -s postgresql | grep Version | cut -f2 -d':'")).strip()[:3]
    P_PG_HOME = "/usr/lib/postgresql/{0}/bin".format(pg_ver)
except Exception, e:
    P_PG_HOME = "/usr/lib/postgresql/9.1/bin"
    print "[paths.py] Exception setting PostgreSQL path: {0}\nSet paths.P_PG_HOME to '{1}'"\
        .format(e, P_PG_HOME)


def get_path(name, default_path):
    """
    Get a file system path where a service with the given ``name`` resides/runs
    as defined in the user data. For example, to use a custom path for Galaxy,
    set ``galaxy_home: /my/custom/path/to/galaxy`` in the user data. If the custom
    path is not provided, just return the ``default_path``.
    """
    path = None
    try:
        path = misc.load_yaml_file(USER_DATA_FILE).get(name, None)
        if path is None:
            downloaded_pd_file = 'pd.yaml'
            if os.path.exists(downloaded_pd_file):
                path = misc.load_yaml_file(downloaded_pd_file).get(name, default_path)
    except:
        pass
    if not path:
        path = default_path
    return path

P_MOUNT_ROOT = "/mnt"
P_GALAXY_TOOLS = get_path("galaxy_tools", os.path.join(P_MOUNT_ROOT, "galaxyTools")) #NGTODO: Harrd coded to tools and indices?
P_GALAXY_HOME = get_path("galaxy_home", os.path.join(P_GALAXY_TOOLS, "galaxy-central"))
P_GALAXY_DATA = get_path("galaxy_data", os.path.join(P_MOUNT_ROOT, 'galaxyData'))
P_GALAXY_INDICES = get_path("galaxy_indices", os.path.join(P_MOUNT_ROOT, "galaxyIndices"))

IMAGE_CONF_SUPPORT_FILE = os.path.join(P_BASE_INSTALL_DIR, 'imageConfig.yaml')


class PathResolver(object):
    def __init__(self, app):
        self.app = app
    
    @property
    def galaxy_tools(self):
        return P_GALAXY_TOOLS
    
    @property
    def galaxy_home(self):
        return P_GALAXY_HOME    
    
    @property
    def galaxy_data(self):
        galaxy_data_fs = self.app.manager.get_services(svc_role=ServiceRole.GALAXY_DATA)
        return galaxy_data_fs[0].mount_point if not None else P_GALAXY_DATA
   
    @property
    def galaxy_indices(self):
        return P_GALAXY_INDICES
    
    @property
    def pg_home(self):
        return P_PG_HOME
    
    @property
    def psql_dir(self):
        return os.path.join(self.galaxy_data, "pgsql/data")
    
    @property
    def mount_root(self):
        return P_MOUNT_ROOT
    
    @property
    def sge_root(self):
        return P_SGE_ROOT
    
    @property
    def sge_tars(self):
        return P_SGE_TARS
    
    @property
    def sge_cell(self):
        return P_SGE_CELL

    
    

