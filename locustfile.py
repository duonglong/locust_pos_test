# TODO: structuralize this file
from locust import HttpLocust, TaskSet, task
from datetime import datetime
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium import webdriver
import xmlrpclib
import random
import logging
from bs4 import BeautifulSoup
import copy

logger = logging.getLogger(__name__)

# TODO: set those as parameter or read from config file
# Configuration
DATABASE = "GS_ERP_P1_GOLIVE_UAT_BLANK"
PORT = 80
HOST = "localhost"
ADMIN_USER = "superadmin"
ADMIN_PASSWORD = "123456"
AMOUNT_PAID = 99999
DEFAULT_PASSWORD = '123456'
LINE_PER_ORDER = 6

# WEB DRIVER
# CHROME_DRIVER_PATH = '/Users/longdt/chromedriver'
WEBDRIVER_OPTIONS = FirefoxOptions()
WEBDRIVER_OPTIONS.add_argument('--headless')
WEBDRIVER_OPTIONS.add_argument('--disable-grpu')

# ODOO DATA
USER_CREDENTIALS = []
POS_CONFIGS = []


def load_users():
    """ Populate user list """
    outlet_datas = RPCProxy().get('br_multi_outlet.outlet').search_read([[]], {"fields": ["id", "oultet_pic1", "oultet_pic2", "oultet_pic3"], "order": "name", "limit": 1})
    for outlet in outlet_datas:
        config = RPCProxy().get('pos.config').search_read([[('outlet_id', '=', outlet['id'])]], {"fields": ["id", "pricelist_id"], "order": "name"})
        user_id = outlet['oultet_pic1'] and outlet['oultet_pic1'][0] or outlet['oultet_pic2'] and outlet['oultet_pic2'][0] or outlet['oultet_pic3'] and outlet['oultet_pic3'][0]
        user = RPCProxy().get('res.users').search_read([[('id', '=', user_id)]], {"fields": ["id", "login"], "order": "name"})
        if user:
            logger.info(">>>> Getting data for user: %s" % user[0]['login'])
            USER_CREDENTIALS.append((user[0]['login'], DEFAULT_PASSWORD, user_id, config[0], outlet))


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


    def get_all_product(self):
        recipes = self.pool.get('br.menu.name.recipe').search_read([[]], {"fields": ['id', 'times', 'applied_for', 'product_qty', 'is_topping', 'categ_ids', 'product_menu_id', 'rule_ids'], 'context': {'load_rule': True}})
        menu_names = self.pool.get('product.product').search_read([[('available_in_pos', '=', True), ('active', '=', True), ('is_menu', '=', True)]], {"fields": ["id", "list_price", "categ_id", "is_menu", "product_recipe_lines", "name", "price"], "context": {'pricelist': self.config['pricelist_id'][0], 'load_menu_name': True}})
        products = self.pool.get('product.product').search_read([[('is_menu', '=', False)]], {"fields": ["id", "list_price", "categ_id", "is_menu", "product_recipe_lines", "name", "price"], "context": {'pricelist': self.config['pricelist_id'][0]}})
        recipe_pricelist = self.pool.get('product.pricelist').get_ls_price([[]])
        recipe_pricelist_categ = self.pool.get('product.pricelist').get_ls_price_categ([[]])
        recipe_pricelist.extend(recipe_pricelist_categ)
        recipes_by_id = {}
        products_by_categ_id = {}
        product_by_id = {}
        recipe_price = {}
        for pricelist in recipe_pricelist:
            pricelist_id, _, fix_price, _, recipe_id = pricelist
            if pricelist_id == self.config['pricelist_id'][0]:
                recipe_price[recipe_id] = fix_price
        for menu in menu_names:
            menu['flavours'] = []
            for r in menu['product_recipe_lines']:
                recipes_by_id[r] = {}
                menu['flavours'].append(recipes_by_id[r])
        for p in products:
            products_by_categ_id.setdefault(p['categ_id'][0], [])
            products_by_categ_id[p['categ_id'][0]].append(p)
            product_by_id[p['id']] = p
        for recipe in recipes:
            if recipe['id'] in recipes_by_id:
                if recipe['applied_for'] == 'product' and recipe['rules']:
                    for rule in recipe['rules']:
                        rule['product'] = product_by_id[rule['product_id'][0]]
                elif recipe['applied_for'] == 'category':
                    for categ in recipe['categ_ids']:
                        if categ in products_by_categ_id:
                            prods = products_by_categ_id[categ]
                            recipe['rules'] = [{'id': recipe['id'], 'product': x, 'product_qty': recipe['product_qty']} for x in prods]
                recipe['price'] = recipe_price[recipe['id']] if recipe['id'] in recipe_price else 0
                recipes_by_id[recipe['id']].update(recipe)
        return menu_names

    def close_session(self):
        """ Close POS Session """
        if self.session_id:
            payload = self.create_json_payload(model='pos.session', id=self.session_id[0], signal='close')
            self.client.post('/web/dataset/exec_workflow', json=payload)
            logger.info("Session ID: %s closed" % self.session_id)

    def _prepare_posorder_data(self):
        if not self.session_id:
            self.session_id = self.pos_session_obj.search([[('state', '=', 'opened'), ('user_id', '=', self.user_id)]])
        if not self.session_id:
            pos_session_obj = self.pool.get("pos.session", login=self.user_name, password=DEFAULT_PASSWORD)
            val = self.generate_session()
            logger.info("Creating session for uid: %s" % self.user_id)
            session_id = [pos_session_obj.create([val])]
            self.session_id = session_id
            logger.info("Opened new session (ID: %s)" % session_id)
            pos_session_obj.wkf_action_open([self.session_id])

        orders = []
        uid = "%s-%s" % (datetime.now().strftime("%y%m%d%H%M%S"), self.user_id)
        orders.append(self._get_order_temp(self.session_id, uid))
        return orders

    def generate_session(self):
        vals = {
            'user_id': self.user_id,
            'config_id': self.config_id,
            'outlet_id': self.outlet_id
        }
        return vals

    def _get_order_temp(self, session_id, uid):
        if any([getattr(self, x) is None for x in ['journal_id', 'account_id', 'statement_id', 'outlet_id']]):
            session = self.pos_session_obj.read([session_id], {'fields': ["statement_ids", "outlet_id"]})
            cashregister = self.account_bank_statement_obj.read([[session[0]['statement_ids'][0]]], {'fields': ["journal_id"]})
            account_journal = self.account_journal_obj.read([[cashregister[0]['journal_id'][0]]], {'fields': ["default_debit_account_id"]})
            self.journal_id = account_journal[0]['id']
            self.account_id = account_journal[0]['default_debit_account_id'][0]
            self.statement_id = cashregister[0]['id']
            self.outlet_id = session[0]['outlet_id'][0]
        total, lines = self.get_order_lines()
        return {
            'to_invoice': False,
            'data': {
                'use_voucher': [],
                'creation_date': '2018-08-28 10:30:42',
                'time_spend': 28,
                'user_id': self.user_id,
                'name': 'Order %s' % uid,
                'partner_id': False,
                'amount_paid': AMOUNT_PAID,
                'pos_session_id': session_id[0],
                'lines': lines,
                'statement_ids': [
                    [0, 0, {
                        'journal_id': self.journal_id,
                        'amount': AMOUNT_PAID,
                        'name': '2018-03-14 07:21:16',
                        'account_id': self.account_id,
                        'statement_id': self.statement_id,
                        'voucher_code': False,
                        'unredeem_value': 0,
                    }]
                ],
                'amount_tax': 0,
                'uid': uid,
                'amount_return': AMOUNT_PAID - total,
                'sequence_number': 15,
                'amount_total': total,
                'note': u'',
                'outlet_id': self.outlet_id,
                'start_time': '2018-08-28 10:02:04',
                'discount_payment': {},
                'activity_sequence': 0,
                'invoice_no': uid,
                'fiscal_position_id': False,
                'origin_total': total
            },
            'id': uid
        }

    def get_order_lines(self):
        l = range(len(self.product_list))
        lines = []
        total = 0
        for i in range(LINE_PER_ORDER):
            product = self.product_list[random.choice(l)]
            lines.append([0, 0, {
                'discount': 0,
                'price_unit': product['price'],
                'product_id': product['id'],
                'qty': 1,
                'is_bundle_item': False,
                'bill_amount': False,
                'voucher': [],
                'promotion_id': False,
                'bill_promotion_ids': [],
                'id': i,
                'non_sale': False,
                'product_category_id': False,
                'pricelist_id': self.config['pricelist_id'][0],
                'rate_promotion': False,
                'tax_ids': [[6, False, [1]]],
                'show_in_cart': True,
                'user_promotion': False,
                'bill_type': False,
                'price_flavor': False,
                'is_flavour_item': False,
                'bom_line_id': False,
                'bom_quantity': False,
                'discount_amount': False,
                'product_master_id': False,
                'product_promotion_id': False,
                'error': False,
                'origin_price': product['price'],
                'min_bill_apply': False
            }])
            for flavour in product['flavours']:
                for j in range(flavour['times']):
                    rule = flavour['rules'][random.choice(range(len(flavour['rules'])))]
                    val = {
                        'is_bundle_item': False,
                        'bill_amount': False,
                        'price_unit': flavour['price'],
                        'qty': rule['product_qty'],
                        'voucher': [],
                        'promotion_id': False,
                        'total_qty': rule['product_qty'],
                        'bill_promotion_ids': [],
                        'id': 6,
                        'non_sale': False,
                        'product_category_id': False,
                        'pricelist_id': self.config['pricelist_id'][0],
                        'rate_promotion': False,
                        'tax_ids': [[6, False, [1]]],
                        'show_in_cart': False,
                        'user_promotion': False,
                        'bill_type': False,
                        'price_flavor': flavour['price'],
                        'discount': 0,
                        'is_flavour_item': True,
                        'bom_line_id': rule['id'],
                        'bom_quantity': 1,
                        'discount_amount': False,
                        'product_master_id': product['id'],
                        'product_id': rule['product']['id'],
                        'product_promotion_id': False,
                        'error': False,
                        'origin_price': flavour['price'],
                        'min_bill_apply': False
                    }
                    lines.append([0, 0, val])
            total += product['price']
        return total, lines

    def create_json_payload(self, **params):
        """ Get json payload data """
        return {
            "id": 73407008,  # just a random number
            "jsonrpc": "2.0",
            "method": "call",
            "params": params,
            "csrf_token": self.csrf_token
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
        browser = webdriver.PhantomJS()
        try:
            browser = webdriver.Firefox(firefox_options=WEBDRIVER_OPTIONS)
            name, value = self.client.cookies.get_dict().items()[0]
            browser.get(self.client.base_url)
            browser.add_cookie({'name': name, 'value': value, 'domain': HOST, 'path': '/', 'expires': u'Web, 03 Jul 2018 18:41:11 GMT', 'expiry': 1530603671})
            browser.get(self.client.base_url + "/pos/web")
            browser.quit()
        except Exception as e:
            logger.info(str(e))
            pass

    def set_crfs_token(self):
        logger.info("Getting CRFS TOKEN")
        html = self.client.get("/web?db=%s" % DATABASE).content
        soup = BeautifulSoup(html, "html.parser")
        inp = soup.find("input", {"name": 'csrf_token'})
        self.csrf_token = inp.attrs['value']
        logger.info(">>>> %s" %self.csrf_token)


    def login(self, login, passw, user_id, config, outlet):
        self.set_crfs_token()
        self.client.post("/web/login", {'login': login, 'password': passw, 'db': DATABASE, 'csrf_token': self.csrf_token})
        self.user_name = login
        self.user_id = user_id
        self.config_id = config['id']
        self.outlet_id = outlet['id']
        self.outlet = outlet
        self.config = config
        self.product_list = self.get_all_product()

    def logout(self):
        self.client.post("/web/session/logout", {'csrf_token': self.csrf_token})


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
            login, passw, user_id, config, outlet = USER_CREDENTIALS.pop()
            self.client.action.login(login, passw, user_id, config, outlet)
            logger.info("User %s logged in" % login)

    def logout(self):
        """ Logout when stop TaskSet """
        if hasattr(self.client, 'user_name'):
            logger.info("User %s logged out" % self.client.user_name)
        if hasattr(self.client, 'action'):
            self.client.action.close_session()
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
    min_wait = 20000
    max_wait = 30000


load_users()

if __name__ == '__main__':
    for i in range(1):
        WebsiteUser().run()
