# TODO: structuralize this file
import logging
import random
from datetime import datetime
from xmlrpc import client as xmlrpclib

from bs4 import BeautifulSoup
from locust import HttpLocust, TaskSet, task, between
from selenium import webdriver
from selenium.webdriver.firefox.options import Options as FirefoxOptions

logger = logging.getLogger(__name__)

# TODO: set those as parameter or read from config file
# Configuration
DATABASE = "ampm_live_1703_2"
PORT = 9000
HOST = "103.17.211.60"
ADMIN_USER = "superadmin"
ADMIN_PASSWORD = "Onnet"
AMOUNT_PAID = 99999
DEFAULT_PASSWORD = 'Onnet'
LINE_PER_ORDER = 6

# WEB DRIVER
# CHROME_DRIVER_PATH = '/Users/longdt/chromedriver'
WEBDRIVER_OPTIONS = FirefoxOptions()
WEBDRIVER_OPTIONS.add_argument('--headless')
WEBDRIVER_OPTIONS.add_argument('--disable-gpu')
POS_GROUP_ID = 33
# ODOO DATA
USER_CREDENTIALS = []
POS_CONFIG = []


def load_users():
    """ Populate user list """
    system_users = [3, 5, 6, 4, 1]
    users = RPCProxy().get('res.users').search_read([[('groups_id', 'in', [POS_GROUP_ID]), ('id', 'not in', system_users), ('active', '=', True)]],
                                                    {"fields": ["id", "login"], "order": "name"})
    for user in users:
        USER_CREDENTIALS.append((user['login'], DEFAULT_PASSWORD, user['id']))


def load_pos_config():
    p = RPCProxy()
    all_session = p.get('pos.session').search_read([[('state', '!=', 'closed')]], {'fields': ['config_id']})
    used_config = [x['config_id'][0] for x in all_session]
    POS_CONFIG.extend(p.get('pos.config').search([[('id', 'not in', used_config)]]))


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
        return lambda *args, **kwargs: self.rpc.execute_kw(DATABASE, self.uid, self.password, self.model, name, *args,
                                                           **kwargs)


class RPCProxy(object):
    models = {}

    def get(self, model, login=ADMIN_USER, password=ADMIN_PASSWORD):
        self.models.setdefault(login, {})
        self.models[login][model] = RPCProxyOne(model, login=login, password=password)
        return self.models[login][model]


class PosAction(object):
    def __init__(self, pool, client):
        self.pool = pool
        self.client = client
        self.user_name = None
        self.user_id = None
        self.config_id = None
        self.session_id = None
        self.journal_id = None
        self.account_id = None
        self.statement_id = None

        self.pos_config_obj = self.pool.get('pos.config')
        self.pos_session_obj = self.pool.get('pos.session')
        self.account_bank_statement_obj = self.pool.get('account.bank.statement')
        self.account_journal_obj = self.pool.get('account.journal')
        self.res_users_obj = self.pool.get('res.users')
        self.product_list = self.get_all_product()
        self.set_csrf_token()

    def create_user(self):
        # TODO: handle those fixed data ?
        # TODO: find more reliable way to create login name
        username = "user_%s%s" % (datetime.now().strftime("%Y%m%d%H%M%S"), random.randint(0, 100000))
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
        user_id = self.res_users_obj.create([val])
        return username, DEFAULT_PASSWORD, user_id

    def create_pos_config(self):
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
        config_id = self.pos_config_obj.create([val])
        return config_id

    def get_all_product(self):
        return self.pool.get('product.product').search_read([[('available_in_pos', '=', True), ('active', '=', True)]],
                                                            {"fields": ["id", "list_price"]})

    def close_session(self):
        """ Close POS Session """
        if self.session_id:
            payload = self.create_json_payload(model='pos.session', id=self.session_id[0], signal='close')
            self.client.post('/web/dataset/exec_workflow', json=payload)
            logger.info("Session ID: %s closed" % self.session_id)

    def _prepare_posorder_data(self):
        if not self.session_id:
            self.session_id = self.pos_session_obj.search(
                [[('state', 'in', ('opened', 'opening_control')), ('user_id', '=', self.user_id)]])
        session_id = self.session_id

        if not session_id:
            pos_session_obj = self.pool.get("pos.session", login=self.user_name, password=DEFAULT_PASSWORD)
            val = self.generate_session()
            logger.info("Creating session for uid: %s" % self.user_id)
            session_id = [pos_session_obj.create([val])]
            self.session_id = session_id
            logger.info("Opened new session (ID: %s)" % session_id)
            pos_session_obj.action_pos_session_open([session_id])
        else:
            session = self.pos_session_obj.search_read([[('id', 'in', session_id)]])[0]
            if session['state'] == 'opening_control':
                self.pos_session_obj.action_pos_session_open([session_id])
        orders = []
        uid = "%s-%s" % (datetime.now().strftime("%y%m%d%H%M%S"), self.user_id)
        orders.append(self._get_order_temp(session_id, uid))
        return orders

    def generate_session(self):
        if not self.config_id:
            self.config_id = self.get_pos_config_id()
        vals = {
            'user_id': self.user_id,
            'config_id': self.config_id
        }
        return vals

    def get_pos_config_id(self):
        if len(POS_CONFIG) > 0:
            pos_config_id = POS_CONFIG.pop()
        else:
            pos_config_id = self.create_pos_config()
        if not pos_config_id:
            # Raise error
            pass
        return pos_config_id

    def _get_order_temp(self, session_id, uid):
        # if any([getattr(self, x) is None for x in ['journal_id', 'account_id', 'statement_id']]):
        #     session = self.pos_session_obj.read([session_id], {'fields': ["statement_ids"]})
        #     cashregister = self.account_bank_statement_obj.read([[session[0]['statement_ids'][0]]], {'fields': ["journal_id"]})
        #     account_journal = self.account_journal_obj.read([[cashregister[0]['journal_id'][0]]], {'fields': ["default_debit_account_id"]})
        #     self.journal_id = account_journal[0]['id']
        #     self.account_id = account_journal[0]['default_debit_account_id'][0]
        #     self.statement_id = cashregister[0]['id']
        # total, lines = self.get_order_lines()
        # return {
        #     'to_invoice': False,
        #     'data': {
        #         'user_id': self.user_id,
        #         'name': 'Order %s' % uid,
        #         'partner_id': False,
        #         'amount_paid': AMOUNT_PAID,
        #         'pos_session_id': session_id[0],
        #         'lines': lines,
        #         'statement_ids': [
        #             [0, 0, {
        #                 'journal_id': self.journal_id,
        #                 'amount': AMOUNT_PAID,
        #                 'name': '2018-03-14 07:21:16',
        #                 'account_id': self.account_id,
        #                 'statement_id': self.statement_id
        #             }]
        #         ],
        #         'amount_tax': 0,
        #         'uid': uid,
        #         'amount_return': AMOUNT_PAID - total,
        #         'sequence_number': 15,
        #         'amount_total': total
        #     },
        #     'id': uid
        # }
        return {'id': uid,
                'data': {
                    'name': 'ORDER %s' % uid,
                    'amount_paid': 9.9,
                    'amount_total': 9.9,
                    'amount_tax': 0,
                    'amount_return': 0,
                    'lines': [
                        [0, 0,
                         {'qty': 1,
                          'price_unit': 9.9,
                          'discount': 0,
                          'product_id': 19185, 'tax_ids': [[6, False, []]],
                          'id': 1, 'pack_lot_ids': [],
                          'line_reference': 'line12867065000211584329416164',
                          'product_uom': 62, 'standard_price': 9.9,
                          'base_price': 9.9, 'special_rule_id': None,
                          'special_loyalty_point': 0, 'special_point_per_value': 0,
                          'special_point_per_qty': 0, 'point_rounding': 0,
                          'loyalty_point': 0,
                          'show_discount': 1.7763568394002505e-15,
                          'real_discount': 1.7763568394002505e-15,
                          'returned_qty': 0, 'refund_line_ref': '',
                          'promotion_discount_value': None, 'promotion_id': None,
                          'promotion_rule': None, 'voucher_id': None}
                         ]
                    ],
                    'statement_ids': [
                        [0, 0,
                         {'name': '2020-03-31 03:30:32',
                          'statement_id': 64348,
                          'account_id': 1084,
                          'journal_id': 8,
                          'amount': 9.9,
                          'payment_ref': '',
                          'detail_desc': ''}
                         ]
                    ],
                    'pos_session_id': session_id[0],
                    'pricelist_id': 2,
                    'partner_id': False,
                    'user_id': 1,
                    'uid': '12867-065-0002',
                    'sequence_number': 1,
                    'creation_date': '2020-03-31T03:30:32.173Z',
                    'fiscal_position_id': False,
                    'customer_name': None,
                    'customer_nric': None,
                    'customer_addr': None,
                    'pharmacist': None,
                    'loyalty_program_id': None,
                    'reward_rule_id': None,
                    'reward_point': 0,
                    'redeem_number': 0,
                    'order_point': 0,
                    'is_voided': False,
                    'is_refunded': False,
                    'applied_birthday': False,
                    'voucher_ids': [],
                    'suspended_order_id': None
                },
                'to_invoice': False
                }

    def get_order_lines(self):
        l = range(len(self.product_list))
        lines = []
        total = 0
        for i in range(LINE_PER_ORDER):
            product = self.product_list[random.choice(l)]
            lines.append([0, 0, {
                'discount': 0,
                'price_unit': product['list_price'],
                'product_id': product['id'],
                'qty': 1
            }])
            total += product['list_price']
        return total, lines

    def create_json_payload(self, **params):
        """ Get json payload data """
        return {
            "id": 73407008,  # just a random number
            "jsonrpc": "2.0",
            "method": "call",
            "params": params
        }

    def create_from_ui(self):
        """ Call to odoo's create_from_ui function """
        posorder_data = [self._prepare_posorder_data()]
        payload = self.create_json_payload(model='pos.order', method='create_from_ui', args=posorder_data, kwargs={})
        pos_res = self.client.post('/web/dataset/call_kw/pos.order/create_from_ui', json=payload)
        order_id = eval(pos_res.text)['result'][0]
        logger.info("Created Order (ID: %s)" % order_id)

    def load_page(self):
        """ Act like browser and load POS screen """
        try:
            browser = webdriver.Firefox(firefox_options=WEBDRIVER_OPTIONS)
            name, value = self.client.cookies.get_dict().items()[0]
            browser.get(self.client.base_url)
            browser.add_cookie(
                {'name': name, 'value': value, 'domain': HOST, 'path': '/', 'expires': u'Web, 03 Jul 2018 18:41:11 GMT',
                 'expiry': 1530603671})
            browser.get(self.client.base_url + "/pos/web")
            browser.quit()
        except Exception as e:
            logger.info(str(e))
            pass

    def set_csrf_token(self):
        content = self.client.get('/web?db=%s' % DATABASE).text
        soup = BeautifulSoup(content, features="html.parser")
        token = soup.find('input', {'name': 'csrf_token'})
        self.csrf_token = token.attrs['value']

    def login(self, login, passw, user_id):
        self.client.post("/web/login",
                         {'login': login, 'password': passw, 'db': DATABASE, 'csrf_token': self.csrf_token})
        self.user_name = login
        self.user_id = user_id

    def logout(self):
        self.client.post("/web/session/logout", {})


class UserBehavior(TaskSet):
    def on_start(self):
        """ Is called when the TaskSet is starting """
        self.set_client_action()
        self.login()
        # self.client.action.load_page()

    def on_stop(self):
        """ Is called when the TaskSet is stopped """
        self.logout()

    def login(self):
        """ Call to host's login action """
        if len(USER_CREDENTIALS) > 0:
            login, passw, user_id = USER_CREDENTIALS.pop()
        else:
            login, passw, user_id = self.client.action.create_user()
        self.client.action.login(login, passw, user_id)
        logger.info("User %s logged in" % login)

    def logout(self):
        """ Logout when stop TaskSet """
        if hasattr(self.client, 'user_name'):
            logger.info("User %s logged out" % self.client.user_name)
        if hasattr(self.client, 'action'):
            # self.client.action.close_session()
            self.client.action.logout()
        self.client.close()

    def set_client_action(self):
        pool = RPCProxy()
        self.client.action = PosAction(pool, self.client)

    @task(1)
    def create_from_ui(self):
        """ Call to odoo's create_from_ui function """
        try:
            self.client.action.create_from_ui()
        except Exception as e:
            logger.info(str(e))
            pass


class WebsiteUser(HttpLocust):
    task_set = UserBehavior
    host = 'http://%s:%s' % (HOST, PORT)
    wait_time = between(1, 3)


load_users()
load_pos_config()

if __name__ == '__main__':
    for i in range(10):
        WebsiteUser().run()
