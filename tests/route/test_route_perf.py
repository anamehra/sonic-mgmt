import pytest
import json
import logging
import time
from datetime import datetime
from tests.common.utilities import wait_until
from tests.common import config_reload
from tests.common.helpers.assertions import pytest_assert

CRM_POLL_INTERVAL = 1
CRM_DEFAULT_POLL_INTERVAL = 300

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.device_type('vs')
]

logger = logging.getLogger(__name__)

ROUTE_TABLE_NAME = 'ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY'

@pytest.fixture(autouse=True)
def ignore_expected_loganalyzer_exceptions(enum_rand_one_per_hwsku_frontend_hostname, loganalyzer):
    """
        Ignore expected failures logs during test execution.

        The route_checker script will compare routes in APP_DB and ASIC_DB, and an ERROR will be
        recorded if mismatch. The testcase will add 10,000 routes to APP_DB, and route_checker may
        detect mismatch during this period. So a new pattern is added to ignore possible error logs.

        Args:
            duthost: DUT fixture
            loganalyzer: Loganalyzer utility fixture
    """
    ignoreRegex = [
        ".*ERR route_check.py:.*",
        ".*ERR.* \'routeCheck\' status failed.*"
    ]
    if loganalyzer:
        # Skip if loganalyzer is disabled
        loganalyzer[enum_rand_one_per_hwsku_frontend_hostname].ignore_regex.extend(ignoreRegex)

@pytest.fixture(params=[4, 6])
def ip_versions(request):
    """
    Parameterized fixture for IP versions.
    """
    yield request.param

@pytest.fixture(scope='function', autouse=True)
def reload_dut(duthosts, enum_rand_one_per_hwsku_frontend_hostname, request):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    yield
    if request.node.rep_call.failed:
        #Issue a config_reload to clear statically added route table and ip addr
        logging.info("Reloading config..")
        config_reload(duthost)

@pytest.fixture(scope="module", autouse=True)
def set_polling_interval(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """ Set CRM polling interval to 1 second """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    wait_time = 2
    duthost.command("crm config polling interval {}".format(CRM_POLL_INTERVAL))
    logger.info("Waiting {} sec for CRM counters to become updated".format(wait_time))
    time.sleep(wait_time)
    yield
    duthost.command("crm config polling interval {}".format(CRM_DEFAULT_POLL_INTERVAL))
    logger.info("Waiting {} sec for CRM counters to become updated".format(wait_time))
    time.sleep(wait_time)

def prepare_dut(duthost, intf_neighs):
    for intf_neigh in intf_neighs:
        # Set up interface
        duthost.config_interface_cmd('add', intf_neigh['interface'], intf_neigh['ip'])
        # Set up neighbor
        duthost.ip_neigh_replace_cmd(intf_neigh['neighbor'], intf_neigh['mac'], intf_neigh['interface'])

def cleanup_dut(duthost, intf_neighs):
    for intf_neigh in intf_neighs:
        # Delete neighbor
        duthost.ip_neigh_add_or_del_cmd('del', intf_neigh['neighbor'], intf_neigh['interface'])
        # remove interface
        duthost.config_interface_cmd('remove', intf_neigh['interface'], intf_neigh['ip'])

def generate_intf_neigh(asichost, num_neigh, ip_version):
    interfaces = asichost.show_interface(command='status')['ansible_facts']['int_status']
    up_interfaces = []
    for intf, values in interfaces.items():
        if values['admin_state'] == 'up' and values['oper_state'] == 'up':
            up_interfaces.append(intf)
    if not up_interfaces:
        raise Exception('DUT does not have up interfaces')

    # Generate interfaces and neighbors
    intf_neighs = []
    str_intf_nexthop = {'ifname':'', 'nexthop':''}

    idx_neigh = 0
    for itfs_name in up_interfaces:
        if not itfs_name.startswith("PortChannel") and interfaces[itfs_name]['vlan'].startswith("PortChannel"):
            continue
        if interfaces[itfs_name]['vlan'] == 'trunk':
            continue
        if ip_version == 4:
            intf_neigh = {
                'interface' : itfs_name,
                'ip' : '10.%d.0.1/24' % (idx_neigh + 1),
                'neighbor' : '10.%d.0.2' % (idx_neigh + 1),
                'mac' : '54:54:00:ad:48:%0.2x' % idx_neigh
            }
        else:
            intf_neigh = {
                'interface' : itfs_name,
                'ip' : '%x::1/64' % (0x2000 + idx_neigh),
                'neighbor' : '%x::2' % (0x2000 + idx_neigh),
                'mac' : '54:54:00:ad:48:%0.2x' % idx_neigh
            }

        intf_neighs.append(intf_neigh)
        if idx_neigh == 0:
            str_intf_nexthop['ifname'] += intf_neigh['interface']
            str_intf_nexthop['nexthop'] += intf_neigh['neighbor']
        else:
            str_intf_nexthop['ifname'] += ',' + intf_neigh['interface']
            str_intf_nexthop['nexthop'] += ',' + intf_neigh['neighbor']
        idx_neigh += 1
        if idx_neigh == num_neigh:
            break

    if not intf_neighs:
        raise Exception('DUT does not have interfaces available for test')

    return intf_neighs, str_intf_nexthop

def generate_route_file(duthost, prefixes, str_intf_nexthop, dir, op):
    route_data = []
    for prefix in prefixes:
        key = 'ROUTE_TABLE:' + prefix
        route = {}
        route['ifname'] = str_intf_nexthop['ifname']
        route['nexthop'] = str_intf_nexthop['nexthop']
        route_command = {}
        route_command[key] = route
        route_command['OP'] = op
        route_data.append(route_command)

    # Copy json file to DUT
    duthost.copy(content=json.dumps(route_data, indent=4), dest=dir, verbose=False)


def exec_routes(duthost, enum_rand_one_asic_index, prefixes, str_intf_nexthop, op):
    # Create a tempfile for routes
    route_file_dir = duthost.shell('mktemp')['stdout']

    # Generate json file for routes
    generate_route_file(duthost, prefixes, str_intf_nexthop, route_file_dir, op)

    # Check the number of routes in ASIC_DB
    asichost = duthost.asic_instance(enum_rand_one_asic_index)
    start_num_route = asichost.count_routes(ROUTE_TABLE_NAME)

    # Calculate timeout as a function of the number of routes
    route_timeout = max(len(prefixes) / 250, 1) # Allow at least 1 second even when there is a limited number of routes

    # Calculate expected number of route and record start time
    if op == 'SET':
        expected_num_routes = start_num_route + len(prefixes)
    elif op == 'DEL':
        expected_num_routes = start_num_route - len(prefixes)
    else:
        pytest.fail('Operation {} not supported'.format(op))
    start_time = datetime.now()
    
    # Apply routes with swssconfig
    json_name = '/dev/stdin < {}'.format(route_file_dir)
    result = duthost.docker_exec_swssconfig(json_name, 'swss', enum_rand_one_asic_index)

    if result['rc'] != 0:
        pytest.fail('Failed to apply route configuration file: {}'.format(result['stderr']))
    
    # Wait until the routes set/del applys to ASIC_DB
    def _check_num_routes(expected_num_routes):
        # Check the number of routes in ASIC_DB
        return asichost.count_routes(ROUTE_TABLE_NAME) == expected_num_routes
    
    if not wait_until(route_timeout, 0.5, 0, _check_num_routes, expected_num_routes):
        pytest.fail('failed to add routes within time limit')

    # Record time when all routes show up in ASIC_DB
    end_time = datetime.now()

    # Check route entries are correct
    asic_route_keys = asichost.get_route_key(ROUTE_TABLE_NAME)
    asic_prefixes = []
    for key in asic_route_keys:
        json_obj = key[len(ROUTE_TABLE_NAME) + 1 : ]
        asic_prefixes.append(json.loads(json_obj)['dest'])
    if op == 'SET':
        assert all(prefix in asic_prefixes for prefix in prefixes)
    elif op == 'DEL':
        assert all(prefix not in asic_prefixes for prefix in prefixes)
    else:
        pytest.fail('Operation {} not supported'.format(op))

    # Retuen time used for set/del routes
    return (end_time - start_time).total_seconds()

def test_perf_add_remove_routes(duthosts, enum_rand_one_per_hwsku_frontend_hostname, request, ip_versions, enum_rand_one_asic_index):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asichost = duthost.asic_instance(enum_rand_one_asic_index)
    # Number of routes for test
    set_num_routes = request.config.getoption("--num_routes")

    # Generate interfaces and neighbors
    NUM_NEIGHS = 50 # Update max num neighbors for multi-asic
    intf_neighs, str_intf_nexthop = generate_intf_neigh(asichost, NUM_NEIGHS, ip_versions)
   
    route_tag = "ipv{}_route".format(ip_versions)
    used_routes_count = asichost.count_crm_resources("main_resources", route_tag, "used")
    avail_routes_count = asichost.count_crm_resources("main_resources", route_tag, "available")
    pytest_assert(avail_routes_count, "CRM main_resources data is not ready within adjusted CRM polling time {}s".\
            format(CRM_POLL_INTERVAL))
    
    num_routes = min(avail_routes_count, set_num_routes)
    logger.info("IP route utilization before test start: Used: {}, Available: {}, Test count: {}"\
        .format(used_routes_count, avail_routes_count, num_routes))

    # Generate ip prefixes of routes
    if (ip_versions == 4):
        prefixes = ['%d.%d.%d.%d/%d' % (101 + int(idx_route / 256 ** 2), int(idx_route / 256) % 256, idx_route % 256, 0, 24)
                    for idx_route in range(num_routes)]
    else:
        prefixes = ['%x:%x:%x::/%d' % (0x3000 + int(idx_route / 65536), idx_route % 65536, 1, 64)
                    for idx_route in range(num_routes)]
    
    try:
        # Set up interface and interface for routes
        prepare_dut(duthost, intf_neighs)

        # Add routes
        time_set = exec_routes(duthost, enum_rand_one_asic_index, prefixes, str_intf_nexthop, 'SET')
        logger.info('Time to set %d ipv%d routes is %.2f seconds.' % (num_routes, ip_versions, time_set))

        # Remove routes
        time_del = exec_routes(duthost, enum_rand_one_asic_index, prefixes, str_intf_nexthop, 'DEL')
        logger.info('Time to del %d ipv%d routes is %.2f seconds.' % (num_routes, ip_versions, time_del))
    finally:
        cleanup_dut(duthost, intf_neighs)
