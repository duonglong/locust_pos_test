# TODO: structuralize this file

from locust import HttpLocust, TaskSet, task
from datetime import datetime
import xmlrpclib
import logging
import random

logger = logging.getLogger(__name__)

# TODO: set those as parameter or read from config file
# Configuration
DATABASE = "odoo8"
PORT = 8088
HOST = "103.17.211.232"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"
AMOUNT_PAID = 99999
DEFAULT_PASSWORD = '123456'
LINE_PER_ORDER = 6

# ODOO DATA
USER_CREDENTIALS = []
POS_CONFIG = []


def load_users():
    """ Populate user list """
    system_users = [3, 5, 6, 4, 1]
    users = RPCProxy().get('res.users').search_read([[('id', 'not in', system_users)]], {"fields": ["id", "login"], "order": "name"})
    for user in users:
        USER_CREDENTIALS.append((user['login'], DEFAULT_PASSWORD, user['id']))


def load_pos_config():
    p = RPCProxy()
    all_session = p.get('pos.session').search_read([[('state', '!=', 'closed')]], {'fields': ['config_id']})
    used_config = [x['config_id'][0] for x in all_session]
    POS_CONFIG.extend(p.get('pos.config').search([[('id', 'not in', used_config)]]))


def create_user():
    # TODO: handle those fixed data ?
    username = "user_%s" % datetime.now().strftime("%Y%m%d%H%M%S")
    val = {
        'password': DEFAULT_PASSWORD,
        'country_id': 21,
        'alias_parent_model_id': 93,
        'alias_force_thread_id': 7,
        'company_id': 1,
        'type': u'contact',
        'lang': u'en_US',
        'alias_contact': u'everyone',
        'company_ids': [(6, 0, [1])],
        'alias_model_id': 93,
        'groups_id': [(6, 0, [5, 11, 45])],
        'alias_parent_thread_id': 7,
        'active': True,
        'login': username,
        'name': username,
        'email': username,
        'alias_user_id': 1,
        'category_id': [(6, 0, [])]
    }
    user_id = RPCProxy().get('res.users').create([val])
    return username, DEFAULT_PASSWORD, user_id


def create_pos_config():
    # TODO: handle those fixed data ?
    name = "POS_%s" % datetime.now().strftime("%Y%m%d%H%M%S")
    val = {
        'stock_location_id': 12,
        'barcode_customer': u'042*',
        'picking_type_id': 11,
        'company_id': 1,
        'state': 'active',
        'pricelist_id': 1,
        'journal_ids': [(6, 0, [5, 6, 7])],
        'sequence_id': 756,
        'name': name,
        'journal_id': 1,
    }
    config_id = RPCProxy().get('pos.config').create([val])
    return config_id


class RPCProxyOne(object):
    """
    Simple XMLRPC client
    """

    def __init__(self, model, login=ADMIN_USER, password=ADMIN_USER):
        local_url = 'http://%s:%d/xmlrpc/common' % (HOST, PORT)
        rpc = xmlrpclib.ServerProxy(local_url)
        self.uid = rpc.login(DATABASE, login, password)
        if not self.uid:
            raise Exception('User %s failed to login' % login)
        self.password = password
        local_url = 'http://%s:%d/xmlrpc/object' % (HOST, PORT)
        self.rpc = xmlrpclib.ServerProxy(local_url)
        self.model = model

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.rpc.execute_kw(DATABASE, self.uid, self.password, self.model, name, *args, **kwargs)


class RPCProxy(object):
    models = {}

    def get(self, model, login=ADMIN_USER, password=ADMIN_PASSWORD):
        self.models.setdefault(login, {})
        # if model not in self.models[login]:
        self.models[login][model] = RPCProxyOne(model, login=login, password=password)
        return self.models[login][model]


class PosDataGenerator(object):
    def __init__(self, pool):
        # TODO: caching should be done in RPC object
        self.dummy_cache = {}
        self.pool = pool
        self.pos_config_obj = self.pool.get('pos.config')
        self.pos_session_obj = self.pool.get('pos.session')
        self.account_bank_statement_obj = self.pool.get('account.bank.statement')
        self.account_journal_obj = self.pool.get('account.journal')
        self.product_list = self.get_all_product()

    def get_all_product(self):
        return self.pool.get('product.product').search_read([[('available_in_pos', '=', True), ('active', '=', True)]], {"fields": ["id", "list_price"]})

    def generate_data(self, user_data):
        self.user_name, self.user_id = user_data
        self.dummy_cache.setdefault(self.user_id, {})
        pos_orders = self.generate_pos_orders()
        return pos_orders

    def generate_pos_orders(self):
        if 'session_id' not in self.dummy_cache[self.user_id]:
            self.dummy_cache[self.user_id]['session_id'] = self.pos_session_obj.search([[('state', '=', 'opened'), ('user_id', '=', self.user_id)]])
        session_id = self.dummy_cache[self.user_id]['session_id']
        if not session_id:
            pos_session_obj = self.pool.get("pos.session", login=self.user_name, password=DEFAULT_PASSWORD)
            val = self.generate_session()
            logger.info("Creating session for uid: %s" % self.user_id)
            logger.info(val)
            session_id = [pos_session_obj.create([val])]
            self.dummy_cache[self.user_id]['session_id'] = session_id
            logger.info("Opened new session (ID: %s)" % session_id)
            pos_session_obj.open_cb([session_id])

        orders = []
        uid = "%s-%s" % (datetime.now().strftime("%y%m%d%H%M%S"), self.user_id)
        orders.append(self._get_order_temp(session_id, uid))
        return orders

    def generate_session(self):
        if 'config_id' not in self.dummy_cache[self.user_id]:
            self.dummy_cache[self.user_id]['config_id'] = self.get_pos_config_id()
        config_id = self.dummy_cache[self.user_id]['config_id']
        logger.info(self.dummy_cache)
        vals = {
            'user_id': self.user_id,
            'config_id': config_id
        }
        return vals

    def get_pos_config_id(self):
        if len(POS_CONFIG) > 0:
            pos_config_id = POS_CONFIG.pop()
        else:
            pos_config_id = create_pos_config()
        if not pos_config_id:
            # Raise error
            pass
        return pos_config_id

    def _get_order_temp(self, session_id, uid):
        if any([x not in self.dummy_cache[self.user_id] for x in ['journal_id', 'account_id', 'statement_id']]):
            session = self.pos_session_obj.read([session_id], {'fields': ["statement_ids"]})
            cashregister = self.account_bank_statement_obj.read([[session[0]['statement_ids'][0]]], {'fields': ["journal_id"]})
            account_journal = self.account_journal_obj.read([[cashregister[0]['journal_id'][0]]], {'fields': ["default_debit_account_id"]})
            self.dummy_cache[self.user_id]['journal_id'] = account_journal[0]['id']
            self.dummy_cache[self.user_id]['account_id'] = account_journal[0]['default_debit_account_id'][0]
            self.dummy_cache[self.user_id]['statement_id'] = cashregister[0]['id']
        journal_id = self.dummy_cache[self.user_id]['journal_id']
        account_id = self.dummy_cache[self.user_id]['account_id']
        statement_id = self.dummy_cache[self.user_id]['statement_id']
        total, lines = self.get_order_lines()
        return {
            'to_invoice': False,
            'data': {
                'user_id': self.user_id,
                'name': 'Order %s' % uid,
                'partner_id': False,
                'amount_paid': AMOUNT_PAID,
                'pos_session_id': session_id[0],
                'lines': lines,
                'statement_ids': [
                    [0, 0, {
                        'journal_id': journal_id,
                        'amount': AMOUNT_PAID,
                        'name': '2018-03-14 07:21:16',
                        'account_id': account_id,
                        'statement_id': statement_id
                    }]
                ],
                'amount_tax': 0,
                'uid': uid,
                'amount_return': AMOUNT_PAID - total,
                'sequence_number': 15,
                'amount_total': total
            },
            'id': uid
        }

    def get_order_lines(self):
        l = range(len(self.product_list))
        lines = []
        total = 0
        for i in range(LINE_PER_ORDER - 1):
            product = self.product_list[random.choice(l)]
            lines.append([0, 0, {
                'discount': 0,
                'price_unit': product['list_price'],
                'product_id': product['id'],
                'qty': 1
            }])
            total += product['list_price']
        return total, lines


class UserBehavior(TaskSet):
    generator_pool = {}

    def on_start(self):
        """ Is called when the TaskSet is starting """
        self.login()

    def on_stop(self):
        """ Is called when the TaskSet is stopping """
        self.logout()

    def login(self):
        """ Call to host's login action """
        if len(USER_CREDENTIALS) > 0:
            login, passw, user_id = USER_CREDENTIALS.pop()
        else:
            login, passw, user_id = create_user()
        self.client.post("/web/login", {'login': login, 'password': passw, 'db': DATABASE})  # init session ?
        self.client.post("/web/login", {'login': login, 'password': passw, 'db': DATABASE})
        self.client.pos_user_id = user_id
        self.client.pos_user_name = login
        logger.info("User %s logged in" % self.client.pos_user_name)

    def logout(self):
        """ Logout when stop TaskSet """
        logger.info("User %s logged out" % self.client.pos_user_name)
        self.client.post("/web/session/logout", {})

    def get_payload(self):
        """ Get json payload data """
        return {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"model": "pos.order", "method": "create_from_ui", "args": [self.prepare_pos_data()], "kwargs": {}},
            "id": 73407008  # just a random number
        }

    def prepare_pos_data(self):
        """ Generate Pos Order """
        if self.client.pos_user_id not in self.generator_pool:
            pool = RPCProxy()
            posdata_generator = PosDataGenerator(pool)
            self.generator_pool[self.client.pos_user_id] = posdata_generator
        return self.generator_pool[self.client.pos_user_id].generate_data((self.client.pos_user_name, self.client.pos_user_id))

    @task(1)
    def create_from_ui(self):
        """ Call to odoo's create_from_ui function """
        pos_res = self.client.post('/web/dataset/call_kw/pos.order/create_from_ui', json=self.get_payload())
        order_id = eval(pos_res.text)['result'][0]
        logger.info("Created Order (ID: %s)" % order_id)


class WebsiteUser(HttpLocust):
    task_set = UserBehavior
    host = 'http://%s:%s' % (HOST, PORT)
    min_wait = 1000
    max_wait = 3000


load_users()
load_pos_config()

if __name__ == '__main__':
    for i in range(10):
        WebsiteUser().run()
