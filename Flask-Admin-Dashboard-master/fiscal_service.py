"""
Serviço de emissão de documentos fiscais via Focus NFe.

MODO ATUAL: homologação (sandbox / teste), sem custo.
Quando o cliente contratar um plano pago, basta:
  1. Trocar FOCUS_NFE_AMBIENTE para 'producao' no config.py
  2. Colocar o token de produção em FOCUS_NFE_TOKEN

IMPORTANTE (falar com o contador antes de ir pra produção):
  - Os códigos fiscais abaixo (CST/CSOSN, CFOP, ICMS) assumem Simples Nacional.
  - Cada produto deveria ter um NCM real cadastrado (hoje usamos um genérico).
  - Emitir nota com dado fiscal errado gera multa/retrabalho — não é só "bug de sistema".
"""
import time
import uuid

import requests
from flask import current_app


def _get_credentials():
    ambiente = current_app.config.get('FOCUS_NFE_AMBIENTE', 'homologacao')
    token = current_app.config.get('FOCUS_NFE_TOKEN')
    base_url = (
        'https://api.focusnfe.com.br'
        if ambiente == 'producao'
        else 'https://homologacao.focusnfe.com.br'
    )
    return token, base_url


def _montar_itens(itens_venda):
    """
    itens_venda: lista de dicts {"descricao", "quantidade", "valor_unitario", "ncm"}
    """
    itens = []
    for idx, item in enumerate(itens_venda, start=1):
        valor_total = round(item['quantidade'] * item['valor_unitario'], 2)
        itens.append({
            "numero_item": str(idx),
            "codigo_produto": str(item.get('codigo', idx)),
            "descricao": item['descricao'],
            "codigo_ncm": item.get('ncm', '85260000'),  # placeholder genérico p/ eletrônicos - AJUSTAR com contador
            "cfop": "5102",
            "unidade_comercial": "UN",
            "quantidade_comercial": item['quantidade'],
            "valor_unitario_comercial": item['valor_unitario'],
            "valor_bruto": valor_total,
            "unidade_tributavel": "UN",
            "quantidade_tributavel": item['quantidade'],
            "valor_unitario_tributavel": item['valor_unitario'],
            "icms_origem": "0",
            "icms_situacao_tributaria": "102",  # Simples Nacional, sem permissão de crédito
        })
    return itens


def _chamar_api(tipo, ref, payload):
    """tipo: 'nfce' ou 'nfe'"""
    token, base_url = _get_credentials()
    if not token:
        return {'success': False, 'message': 'Token da Focus NFe não configurado em config.py (FOCUS_NFE_TOKEN).'}

    url = f"{base_url}/v2/{tipo}?ref={ref}"

    try:
        resp = requests.post(url, json=payload, auth=(token, ''), timeout=25)
        data = resp.json() if resp.content else {}
    except requests.RequestException as e:
        return {'success': False, 'message': f'Falha de conexão com a Focus NFe: {e}'}

    status = data.get('status')
    tentativas = 0
    # A Focus NFe processa de forma assíncrona; se ainda estiver processando, consultamos até 4x.
    while status == 'processando_autorizacao' and tentativas < 4:
        time.sleep(2)
        try:
            consulta = requests.get(f"{base_url}/v2/{tipo}/{ref}", auth=(token, ''), timeout=25)
            data = consulta.json() if consulta.content else {}
            status = data.get('status')
        except requests.RequestException:
            break
        tentativas += 1

    if status == 'autorizado':
        return {
            'success': True,
            'status': status,
            'numero': data.get('numero'),
            'serie': data.get('serie'),
            'chave': data.get('chave_nfe') or data.get('chave_nfce'),
            'url_danfe': data.get('caminho_danfe') or data.get('caminho_danfe_pdf'),
            'url_xml': data.get('caminho_xml_nota_fiscal'),
        }

    mensagem_erro = data.get('mensagem_sefaz') or data.get('mensagem') or 'Erro desconhecido ao emitir o documento fiscal.'
    return {'success': False, 'status': status, 'message': mensagem_erro, 'raw': data}


def emitir_nfce(venda, itens_venda, cnpj_emitente):
    """
    venda: objeto Sale (usa venda.id e venda.created_at)
    itens_venda: lista formatada por _montar_itens
    cnpj_emitente: CNPJ da HD Drones (vem do config.py)
    """
    ref = f"nfce-venda-{venda.id}-{uuid.uuid4().hex[:8]}"
    payload = {
        "natureza_operacao": "Venda ao consumidor",
        "data_emissao": venda.created_at.strftime('%Y-%m-%dT%H:%M:%S-03:00'),
        "presenca_comprador": "1",  # operação presencial (balcão)
        "modalidade_frete": "9",    # sem frete
        "cnpj_emitente": cnpj_emitente,
        "valor_produtos": venda.total_amount,
        "valor_total": venda.total_amount,
        "itens": _montar_itens(itens_venda),
    }
    return _chamar_api('nfce', ref, payload)


def emitir_nfe(venda, itens_venda, cnpj_emitente, destinatario=None):
    """
    NF-e exige dados do destinatário (cliente) quando não é venda de balcão a consumidor final anônimo.
    destinatario: dict opcional {"cpf" ou "cnpj", "nome", "endereco": {...}}
    """
    ref = f"nfe-venda-{venda.id}-{uuid.uuid4().hex[:8]}"
    payload = {
        "natureza_operacao": "Venda de mercadoria",
        "data_emissao": venda.created_at.strftime('%Y-%m-%dT%H:%M:%S-03:00'),
        "tipo_documento": "1",
        "finalidade_emissao": "1",
        "consumidor_final": "1" if not destinatario else "0",
        "cnpj_emitente": cnpj_emitente,
        "valor_produtos": venda.total_amount,
        "valor_total": venda.total_amount,
        "itens": _montar_itens(itens_venda),
    }
    if destinatario:
        payload.update(destinatario)

    return _chamar_api('nfe', ref, payload)


def emitir_documento_os(ordem_servico, tipo, cnpj_emitente):
    """
    Emite NFC-e ou NF-e para uma Ordem de Serviço, tratando avaliação + mão de obra
    como um item de serviço avulso.

    AVISO: o correto fiscalmente para mão de obra/serviço é a NFS-e (nota de serviço,
    emitida junto à prefeitura, não à Sefaz). Isso é uma integração separada, por município.
    Por ora, tratamos como item dentro de NFC-e/NF-e só para fins de demonstração.
    """
    valor_total = (ordem_servico.evaluation_fee or 0) + (ordem_servico.maintenance_fee or 0)
    if valor_total <= 0:
        return {'success': False, 'message': 'Ordem de serviço sem valores de avaliação/manutenção lançados.'}

    itens_venda = [{
        'descricao': f"Serviço técnico - OS #{ordem_servico.id} ({ordem_servico.accessories})",
        'quantidade': 1,
        'valor_unitario': valor_total,
        'ncm': '00000000',
    }]

    class _VendaFake:
        id = ordem_servico.id
        created_at = ordem_servico.user and __import__('datetime').datetime.now()
        total_amount = valor_total

    venda_fake = _VendaFake()
    venda_fake.created_at = __import__('datetime').datetime.now()

    if tipo == 'nfce':
        return emitir_nfce(venda_fake, itens_venda, cnpj_emitente)
    return emitir_nfe(venda_fake, itens_venda, cnpj_emitente)