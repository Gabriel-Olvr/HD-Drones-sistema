#!venv/bin/python
import os
from datetime import datetime
from flask import Flask, url_for, redirect, render_template, request, abort, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from flask_security import Security, SQLAlchemyUserDatastore, \
    UserMixin, RoleMixin, login_required, current_user
from flask_security.utils import hash_password
from flask_babel import Babel
import flask_admin
from flask_admin import AdminIndexView, expose
from flask_admin.actions import action
from flask_admin.contrib import sqla
from flask_admin import helpers as admin_helpers
from flask_admin.theme import Bootstrap4Theme
from wtforms import PasswordField
import uuid

import fiscal_service
import pix_service

# === INICIALIZAÇÃO DO APP ===
app = Flask(__name__)
app.config.from_pyfile('config.py')

db = SQLAlchemy(app)

babel = Babel(app, locale_selector=lambda: 'pt_BR')

# === ROTAS PRINCIPAIS ===
@app.route('/', endpoint='home_site')
def home():
    return redirect(url_for('admin.index'))


@app.route('/pdv/venda/<int:sale_id>/pix')
@login_required
def pdv_gerar_pix(sale_id):
    venda = Sale.query.get(sale_id)
    if not venda:
        return jsonify({'success': False, 'message': 'Venda não encontrada.'}), 404

    chave_pix = app.config.get('PIX_KEY')
    if not chave_pix:
        return jsonify({'success': False, 'message': 'Chave Pix não configurada em config.py (PIX_KEY).'}), 400

    nome = app.config.get('PIX_MERCHANT_NAME', 'HANGAR')
    cidade = app.config.get('PIX_MERCHANT_CITY', 'SAO PAULO')

    payload = pix_service.gerar_payload_pix(
        chave_pix, nome, cidade, venda.total_amount, identificador=f'VENDA{venda.id}'
    )
    qr_base64 = pix_service.gerar_qrcode_base64(payload)

    return jsonify({'success': True, 'payload': payload, 'qr_base64': qr_base64})


@app.route('/pdv/venda/<int:sale_id>/emitir', methods=['POST'])
@login_required
def pdv_emitir_nota(sale_id):
    venda = Sale.query.get(sale_id)
    if not venda:
        return jsonify({'success': False, 'message': 'Venda não encontrada.'}), 404

    data = request.get_json(silent=True) or {}
    tipo = data.get('tipo')
    if tipo not in ('nfce', 'nfe'):
        return jsonify({'success': False, 'message': 'Tipo de documento inválido.'}), 400

    cnpj_emitente = app.config.get('EMPRESA_CNPJ', '')
    itens_venda = [{
        'codigo': item.product_id,
        'descricao': item.product.name,
        'quantidade': item.quantity,
        'valor_unitario': item.unit_price,
        'ncm': item.product.ncm,
    } for item in venda.items]

    if tipo == 'nfce':
        resultado = fiscal_service.emitir_nfce(venda, itens_venda, cnpj_emitente)
    else:
        resultado = fiscal_service.emitir_nfe(venda, itens_venda, cnpj_emitente)

    venda.tipo_documento_fiscal = tipo
    venda.status_fiscal = resultado.get('status', 'erro')
    if resultado.get('success'):
        venda.numero_fiscal = resultado.get('numero')
        venda.chave_fiscal = resultado.get('chave')
        venda.url_danfe = resultado.get('url_danfe')
        venda.url_xml_fiscal = resultado.get('url_xml')
        venda.mensagem_fiscal = None
    else:
        venda.mensagem_fiscal = resultado.get('message')
    db.session.commit()

    return jsonify(resultado)


@app.route('/pdv')
@login_required
def pdv():
    produtos = Product.query.all()
    return render_template('pdv.html', produtos=produtos)


@app.route('/pdv/finalizar', methods=['POST'])
@login_required
def pdv_finalizar():
    data = request.get_json(silent=True)
    if not data or not data.get('items'):
        return jsonify({'success': False, 'message': 'Carrinho vazio.'}), 400

    forma_pagamento = data.get('forma_pagamento')
    if forma_pagamento not in ('dinheiro', 'credito', 'debito', 'pix'):
        return jsonify({'success': False, 'message': 'Selecione uma forma de pagamento.'}), 400

    try:
        venda = Sale(seller_id=current_user.id, forma_pagamento=forma_pagamento)
        db.session.add(venda)

        total_venda = 0.0
        total_comissao = 0.0

        for item_data in data['items']:
            produto = Product.query.get(item_data.get('product_id'))
            if not produto:
                raise ValueError('Produto não encontrado.')

            quantidade = int(item_data.get('quantity', 0))
            if quantidade <= 0:
                raise ValueError(f"Quantidade inválida para '{produto.name}'.")

            if produto.stock_quantity < quantidade:
                raise ValueError(f"Estoque insuficiente para '{produto.name}'! Restam: {produto.stock_quantity}")

            # Preço vem sempre do banco (nunca do cliente), evitando manipulação de preço no front-end
            preco_unitario = produto.sale_price
            if preco_unitario < produto.min_price:
                raise ValueError(f"O preço de '{produto.name}' está abaixo do mínimo permitido.")

            if produto.category == 'novo':
                lucro = preco_unitario - produto.cost_price
                comissao = max(0, lucro * 0.01) * quantidade
            elif produto.category == 'seminovo':
                comissao = 50.0 * quantidade
            else:
                comissao = 0.0

            produto.stock_quantity -= quantidade

            sale_item = SaleItem(
                sale=venda,
                product=produto,
                quantity=quantidade,
                unit_price=preco_unitario,
                commission=comissao
            )
            db.session.add(sale_item)

            total_venda += preco_unitario * quantidade
            total_comissao += comissao

        venda.total_amount = total_venda
        venda.total_commission = total_comissao

        db.session.commit()
        return jsonify({'success': True, 'sale_id': venda.id, 'total': total_venda})

    except ValueError as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Erro inesperado ao finalizar a venda.'}), 500


# === MODELOS DE BANCO DE DADOS ===
roles_users = db.Table(
    'roles_users',
    db.Column('user_id', db.Integer(), db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer(), db.ForeignKey('role.id'))
)


class Role(db.Model, RoleMixin):
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

    def __str__(self):
        return self.name


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(255))
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    fs_uniquifier = db.Column(db.String(255), unique=True, nullable=False)
    active = db.Column(db.Boolean(), default=True)
    confirmed_at = db.Column(db.DateTime())
    roles = db.relationship('Role', secondary=roles_users,
                             backref=db.backref('users', lazy='dynamic'))

    def __str__(self):
        return self.email


class ServiceOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    problem_description = db.Column(db.Text, nullable=False)
    accessories = db.Column(db.String(255), nullable=False)
    evaluation_fee = db.Column(db.Float, default=0.0)
    maintenance_fee = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(50), default='Pendente')

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('service_orders', lazy='dynamic'))

    # Campos fiscais (preenchidos após emissão via Focus NFe)
    tipo_documento_fiscal = db.Column(db.String(10))
    status_fiscal = db.Column(db.String(30))
    numero_fiscal = db.Column(db.String(20))
    chave_fiscal = db.Column(db.String(60))
    url_danfe = db.Column(db.String(255))
    mensagem_fiscal = db.Column(db.Text)

    def __str__(self):
        return f"OS #{self.id} - Status: {self.status}"


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # 'novo', 'seminovo', 'peca'
    cost_price = db.Column(db.Float, nullable=False)
    min_price = db.Column(db.Float, nullable=False)
    sale_price = db.Column(db.Float, nullable=False)
    stock_quantity = db.Column(db.Integer, default=0)
    ncm = db.Column(db.String(10), default='88069200')  # drones (250g-7kg c/ câmera) - peças/acessórios têm NCM diferente, confirmar com contador

    def __str__(self):
        return f"{self.name} (Estoque: {self.stock_quantity})"


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=db.func.now())
    total_amount = db.Column(db.Float, default=0.0)
    total_commission = db.Column(db.Float, default=0.0)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    seller = db.relationship('User', backref=db.backref('sales', lazy='dynamic'))

    # Campos fiscais (preenchidos após emissão via Focus NFe)
    tipo_documento_fiscal = db.Column(db.String(10))  # 'nfce' ou 'nfe'
    status_fiscal = db.Column(db.String(30))
    numero_fiscal = db.Column(db.String(20))
    chave_fiscal = db.Column(db.String(60))
    url_danfe = db.Column(db.String(255))
    url_xml_fiscal = db.Column(db.String(255))
    mensagem_fiscal = db.Column(db.Text)

    # Forma de pagamento
    forma_pagamento = db.Column(db.String(20))  # 'dinheiro', 'credito', 'debito', 'pix'

    def __str__(self):
        return f"Venda #{self.id} - R$ {self.total_amount:.2f}"


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    sale = db.relationship('Sale', backref=db.backref('items', lazy='dynamic'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product = db.relationship('Product')
    quantity = db.Column(db.Integer, default=1)
    unit_price = db.Column(db.Float, nullable=False)
    commission = db.Column(db.Float, default=0.0)


# === SETUP FLASK-SECURITY ===
user_datastore = SQLAlchemyUserDatastore(db, User, Role)
security = Security(app, user_datastore)


# === VIEW CUSTOMIZADA DO DASHBOARD (usa templates/admin/custom_index.html) ===
class HangarIndexView(AdminIndexView):
    def is_accessible(self):
        return current_user.is_active and current_user.is_authenticated

    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for('security.login', next=request.url))

    @expose('/')
    def index(self):
        total_produtos = Product.query.count()
        total_os_pendentes = ServiceOrder.query.filter_by(status='Pendente').count()
        vendas_recentes = Sale.query.order_by(Sale.created_at.desc()).limit(5).all()

        contexto = dict(
            total_produtos=total_produtos,
            total_os_pendentes=total_os_pendentes,
            vendas_recentes=vendas_recentes,
        )

        # === MÉTRICAS DO MÊS (visível só para quem tem o papel "dono") ===
        if current_user.has_role('dono'):
            inicio_mes = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            vendas_mes = Sale.query.filter(Sale.created_at >= inicio_mes).all()

            faturamento_mes = sum(v.total_amount for v in vendas_mes)
            qtd_vendas_mes = len(vendas_mes)
            ticket_medio = (faturamento_mes / qtd_vendas_mes) if qtd_vendas_mes else 0
            comissao_total_mes = sum(v.total_commission for v in vendas_mes)

            top_produtos = db.session.query(
                Product.name,
                func.sum(SaleItem.quantity).label('qtd'),
                func.sum(SaleItem.unit_price * SaleItem.quantity).label('total')
            ).join(SaleItem, SaleItem.product_id == Product.id
            ).join(Sale, Sale.id == SaleItem.sale_id
            ).filter(Sale.created_at >= inicio_mes
            ).group_by(Product.id
            ).order_by(func.sum(SaleItem.unit_price * SaleItem.quantity).desc()
            ).limit(5).all()

            ranking_vendedores = db.session.query(
                User.first_name, User.email,
                func.count(Sale.id).label('qtd_vendas'),
                func.sum(Sale.total_amount).label('total_vendido'),
                func.sum(Sale.total_commission).label('total_comissao')
            ).join(Sale, Sale.seller_id == User.id
            ).filter(Sale.created_at >= inicio_mes
            ).group_by(User.id
            ).order_by(func.sum(Sale.total_amount).desc()
            ).all()

            contexto.update(
                is_admin=True,
                faturamento_mes=faturamento_mes,
                qtd_vendas_mes=qtd_vendas_mes,
                ticket_medio=ticket_medio,
                comissao_total_mes=comissao_total_mes,
                top_produtos=top_produtos,
                ranking_vendedores=ranking_vendedores,
            )
        else:
            contexto['is_admin'] = False

        return self.render('admin/custom_index.html', **contexto)


# === VIEWS DO PAINEL ADMIN ===
class MyModelView(sqla.ModelView):
    def is_accessible(self):
        return current_user.is_active and current_user.is_authenticated and current_user.has_role('superuser')

    def inaccessible_callback(self, name, **kwargs):
        if current_user.is_authenticated:
            abort(403)
        return redirect(url_for('security.login', next=request.url))

    edit_modal = True
    create_modal = True
    can_export = True
    can_view_details = True
    details_modal = True


class UserView(MyModelView):
    column_editable_list = ['email', 'first_name', 'last_name']
    column_searchable_list = column_editable_list
    column_exclude_list = ['password', 'fs_uniquifier']
    column_details_exclude_list = column_exclude_list
    column_filters = column_editable_list
    column_labels = {
        'first_name': 'Nome',
        'last_name': 'Sobrenome',
        'email': 'E-mail',
        'active': 'Ativo',
        'confirmed_at': 'Confirmado em',
        'password': 'Senha',
        'roles': 'Papéis',
    }
    form_columns = ['first_name', 'last_name', 'email', 'password', 'active', 'roles']
    form_overrides = {
        'password': PasswordField
    }

    # Garante que novos usuários criados pelo painel tenham fs_uniquifier e senha com hash
    def on_model_change(self, form, model, is_created):
        if is_created:
            if not model.fs_uniquifier:
                model.fs_uniquifier = uuid.uuid4().hex
            if model.password and not model.password.startswith('$'):
                model.password = hash_password(model.password)
            if model.active is None:
                model.active = True
        super().on_model_change(form, model, is_created)


class ServiceOrderView(MyModelView):
    column_list = ['id', 'user', 'problem_description', 'accessories', 'evaluation_fee', 'maintenance_fee', 'status', 'status_fiscal']
    column_searchable_list = ['problem_description', 'accessories']
    column_filters = ['status', 'user']
    form_columns = ['user', 'problem_description', 'accessories', 'evaluation_fee', 'maintenance_fee', 'status']
    form_choices = {
        'status': [
            ('Pendente', 'Pendente'),
            ('Em Andamento', 'Em Andamento'),
            ('Realizada', 'Realizada'),
            ('Cancelada', 'Cancelada'),
        ]
    }
    column_labels = {
        'id': 'Nº',
        'user': 'Responsável',
        'problem_description': 'Descrição do Problema',
        'accessories': 'Acessórios',
        'evaluation_fee': 'Valor da Hora de Avaliação (R$)',
        'maintenance_fee': 'Taxa de Manutenção (R$)',
        'status': 'Status',
        'status_fiscal': 'Status Fiscal',
    }

    def on_model_change(self, form, model, is_created):
        # Regra de negócio: se a manutenção foi realmente feita, a taxa de avaliação é isentada
        # (o cliente só paga a avaliação separadamente se NÃO seguir com o conserto).
        if model.status == 'Realizada' and model.maintenance_fee and model.maintenance_fee > 0:
            model.evaluation_fee = 0.0
        super().on_model_change(form, model, is_created)

    @action('marcar_realizada', 'Marcar como Realizada', 'Confirma que a manutenção foi realizada nas OS selecionadas? (isenta a taxa de avaliação automaticamente)')
    def action_marcar_realizada(self, ids):
        atualizadas = 0
        for os_id in ids:
            ordem = ServiceOrder.query.get(os_id)
            if not ordem:
                continue
            ordem.status = 'Realizada'
            if ordem.maintenance_fee and ordem.maintenance_fee > 0:
                ordem.evaluation_fee = 0.0
            atualizadas += 1
        db.session.commit()
        if atualizadas:
            flash(f"{atualizadas} ordem(ns) de serviço marcada(s) como Realizada.", 'success')

    def _emitir_documento(self, ids, tipo):
        cnpj_emitente = self.admin.app.config.get('EMPRESA_CNPJ', '')
        sucesso, falhas = 0, []

        for os_id in ids:
            ordem = ServiceOrder.query.get(os_id)
            if not ordem:
                continue
            resultado = fiscal_service.emitir_documento_os(ordem, tipo, cnpj_emitente)
            ordem.tipo_documento_fiscal = tipo
            ordem.status_fiscal = resultado.get('status', 'erro')
            if resultado.get('success'):
                ordem.numero_fiscal = resultado.get('numero')
                ordem.chave_fiscal = resultado.get('chave')
                ordem.url_danfe = resultado.get('url_danfe')
                ordem.mensagem_fiscal = None
                sucesso += 1
            else:
                ordem.mensagem_fiscal = resultado.get('message')
                falhas.append(f"OS #{ordem.id}: {resultado.get('message')}")

        db.session.commit()

        if sucesso:
            flash(f"{sucesso} documento(s) fiscal(is) emitido(s) com sucesso.", 'success')
        for erro in falhas:
            flash(erro, 'error')

    @action('emitir_nfce', 'Emitir Cupom Fiscal (NFC-e)', 'Confirma a emissão do Cupom Fiscal para as OS selecionadas?')
    def action_emitir_nfce(self, ids):
        self._emitir_documento(ids, 'nfce')

    @action('emitir_nfe', 'Emitir Nota Fiscal (NF-e)', 'Confirma a emissão da Nota Fiscal para as OS selecionadas?')
    def action_emitir_nfe(self, ids):
        self._emitir_documento(ids, 'nfe')


class ProductView(MyModelView):
    def on_model_change(self, form, model, is_created):
        if model.sale_price < model.min_price:
            raise ValueError("Erro de Segurança: O preço de venda não pode ser menor que o preço mínimo estipulado!")
        super().on_model_change(form, model, is_created)

    column_list = ['name', 'category', 'cost_price', 'min_price', 'sale_price', 'stock_quantity']
    column_searchable_list = ['name', 'category']
    column_filters = ['category']
    form_columns = ['name', 'category', 'cost_price', 'min_price', 'sale_price', 'stock_quantity', 'ncm']
    column_labels = {
        'name': 'Nome',
        'category': 'Categoria',
        'cost_price': 'Preço de Custo',
        'min_price': 'Preço Mínimo',
        'sale_price': 'Preço de Venda',
        'stock_quantity': 'Estoque',
        'ncm': 'NCM',
    }


class SaleView(MyModelView):
    column_list = ['id', 'created_at', 'seller', 'forma_pagamento', 'total_amount', 'total_commission', 'status_fiscal']
    inline_models = (SaleItem,)
    column_labels = {
        'id': 'Nº',
        'created_at': 'Data',
        'seller': 'Vendedor',
        'forma_pagamento': 'Pagamento',
        'total_amount': 'Total',
        'total_commission': 'Comissão',
        'status_fiscal': 'Status Fiscal',
    }

    def on_model_change(self, form, model, is_created):
        total_venda = 0.0
        total_comissao = 0.0

        for item in model.items:
            produto = item.product

            if item.unit_price < produto.min_price:
                raise ValueError(f"Erro: O preço do '{produto.name}' está abaixo do mínimo (R$ {produto.min_price})!")

            if is_created:
                if produto.stock_quantity < item.quantity:
                    raise ValueError(f"Estoque insuficiente para '{produto.name}'! Restam: {produto.stock_quantity}")
                produto.stock_quantity -= item.quantity

            if produto.category == 'novo':
                lucro = item.unit_price - produto.cost_price
                item.commission = max(0, lucro * 0.01) * item.quantity
            elif produto.category == 'seminovo':
                item.commission = 50.0 * item.quantity
            else:
                item.commission = 0.0

            total_venda += (item.unit_price * item.quantity)
            total_comissao += item.commission

        model.total_amount = total_venda
        model.total_commission = total_comissao
        super().on_model_change(form, model, is_created)

    def _emitir_documento(self, ids, tipo):
        cnpj_emitente = self.admin.app.config.get('EMPRESA_CNPJ', '')
        sucesso, falhas = 0, []

        for sale_id in ids:
            venda = Sale.query.get(sale_id)
            if not venda:
                continue

            itens_venda = [{
                'codigo': item.product_id,
                'descricao': item.product.name,
                'quantidade': item.quantity,
                'valor_unitario': item.unit_price,
                'ncm': item.product.ncm,
            } for item in venda.items]

            if tipo == 'nfce':
                resultado = fiscal_service.emitir_nfce(venda, itens_venda, cnpj_emitente)
            else:
                resultado = fiscal_service.emitir_nfe(venda, itens_venda, cnpj_emitente)

            venda.tipo_documento_fiscal = tipo
            venda.status_fiscal = resultado.get('status', 'erro')
            if resultado.get('success'):
                venda.numero_fiscal = resultado.get('numero')
                venda.chave_fiscal = resultado.get('chave')
                venda.url_danfe = resultado.get('url_danfe')
                venda.url_xml_fiscal = resultado.get('url_xml')
                venda.mensagem_fiscal = None
                sucesso += 1
            else:
                venda.mensagem_fiscal = resultado.get('message')
                falhas.append(f"Venda #{venda.id}: {resultado.get('message')}")

        db.session.commit()

        if sucesso:
            flash(f"{sucesso} documento(s) fiscal(is) emitido(s) com sucesso.", 'success')
        for erro in falhas:
            flash(erro, 'error')

    @action('emitir_nfce', 'Emitir Cupom Fiscal (NFC-e)', 'Confirma a emissão do Cupom Fiscal para as vendas selecionadas?')
    def action_emitir_nfce(self, ids):
        self._emitir_documento(ids, 'nfce')

    @action('emitir_nfe', 'Emitir Nota Fiscal (NF-e)', 'Confirma a emissão da Nota Fiscal para as vendas selecionadas?')
    def action_emitir_nfe(self, ids):
        self._emitir_documento(ids, 'nfe')


# === INICIALIZAÇÃO DO ADMIN ===
admin = flask_admin.Admin(
    app,
    'Hangar - Gestão Comercial & PDV',
    index_view=HangarIndexView(),
    theme=Bootstrap4Theme(swatch='darkly', base_template='my_master.html')
)

admin.add_view(UserView(User, db, menu_icon_type='fa', menu_icon_value='fa-users', name="Usuários"))
admin.add_view(MyModelView(Role, db, menu_icon_type='fa', menu_icon_value='fa-id-badge', name="Papéis"))
admin.add_view(ProductView(Product, db, menu_icon_type='fa', menu_icon_value='fa-box', name="Estoque & Produtos"))
admin.add_view(ServiceOrderView(ServiceOrder, db, menu_icon_type='fa', menu_icon_value='fa-wrench', name="Ordens de Serviço"))
admin.add_view(SaleView(Sale, db, menu_icon_type='fa', menu_icon_value='fa-cash-register', name="PDV & Vendas"))


# === CONTEXTO DE SEGURANÇA (usa o theme real do Flask-Admin) ===
@security.context_processor
def security_context_processor():
    return dict(
        admin_base_template=admin.theme.base_template,
        admin_view=admin.index_view,
        h=admin_helpers,
        get_url=url_for,
        theme=admin.theme
    )


# === CONTEXTO GLOBAL PARA TODAS AS PÁGINAS (INCLUINDO O PDV) ===
@app.context_processor
def inject_globals():
    return dict(
        admin_base_template=admin.theme.base_template,
        admin_view=admin.index_view,
        h=admin_helpers,
        get_url=url_for,
        theme=admin.theme
    )


# === DADOS DE EXEMPLO DO BANCO ===
def build_sample_db():
    with app.app_context():
        db.drop_all()
        db.create_all()

        user_role = Role(name='user')
        super_user_role = Role(name='superuser')
        dono_role = Role(name='dono', description='Vê métricas gerenciais do Dashboard')
        db.session.add(user_role)
        db.session.add(super_user_role)
        db.session.add(dono_role)
        db.session.commit()

        user_datastore.create_user(
            first_name='Admin',
            email='admin@admin.com',
            password=hash_password('admin'),
            active=True,
            fs_uniquifier=uuid.uuid4().hex,
            roles=[user_role, super_user_role, dono_role]
        )

        user_datastore.create_user(
            first_name='Vendedor',
            email='vendedor@hangar.com.br',
            password=hash_password('vendedor123'),
            active=True,
            fs_uniquifier=uuid.uuid4().hex,
            roles=[user_role]
        )

        prod_teste = Product(
            name="Drone DJI Phantom (Seminovo)",
            category="seminovo",
            cost_price=2500.0,
            min_price=2900.0,
            sale_price=3200.0,
            stock_quantity=5
        )
        db.session.add(prod_teste)
        db.session.commit()


# === EXECUÇÃO DA APLICAÇÃO ===
if __name__ == '__main__':
    app_dir = os.path.realpath(os.path.dirname(__file__))
    database_path = os.path.join(app_dir, app.config['DATABASE_FILE'])
    if not os.path.exists(database_path):
        build_sample_db()

    # Rodando em HTTPS local
    app.run(debug=True, host='0.0.0.0', port=5000, ssl_context='adhoc')