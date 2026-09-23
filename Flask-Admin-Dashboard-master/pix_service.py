"""
Gerador de Pix estático (padrão "BR Code" do Banco Central do Brasil).

Isso é um padrão ABERTO (EMV QR Code / Manual de Padrões para Iniciação do Pix
do BCB) — não depende de nenhuma API paga, banco ou credencial. Só precisa
da chave Pix real da loja, configurada em config.py.

Referência do padrão: Banco Central do Brasil - Manual de Padrões para
Iniciação do Pix (especificação pública).
"""
import base64
import io

import qrcode


def _crc16_ccitt(payload: str) -> str:
    """CRC16-CCITT (polinômio 0x1021, valor inicial 0xFFFF) — exigido no final do payload Pix."""
    crc = 0xFFFF
    for char in payload.encode('utf-8'):
        crc ^= (char << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return format(crc, '04X')


def _tlv(id_: str, value: str) -> str:
    """Formata um campo no padrão TLV (Tag-Length-Value) usado pelo BR Code."""
    length = str(len(value)).zfill(2)
    return f"{id_}{length}{value}"


def gerar_payload_pix(chave_pix: str, nome_recebedor: str, cidade_recebedor: str,
                       valor: float, identificador: str = 'HANGAR'):
    """
    Monta o payload "copia e cola" do Pix estático.

    chave_pix: chave Pix da loja (CPF, CNPJ, e-mail, telefone ou chave aleatória)
    nome_recebedor: até 25 caracteres, sem acentuação é mais seguro
    cidade_recebedor: até 15 caracteres
    valor: valor da cobrança (ex: 150.00)
    identificador: até 25 caracteres, aparece no app do pagador (ex: nº da venda)
    """
    nome_recebedor = (nome_recebedor or 'HANGAR')[:25]
    cidade_recebedor = (cidade_recebedor or 'SAO PAULO')[:15]
    identificador = (identificador or '***')[:25]

    merchant_account_info = (
        _tlv('00', 'br.gov.bcb.pix') +
        _tlv('01', chave_pix)
    )

    additional_data = _tlv('05', identificador)

    payload = (
        _tlv('00', '01') +                              # Payload Format Indicator
        _tlv('26', merchant_account_info) +              # Merchant Account Info (Pix)
        _tlv('52', '0000') +                              # Merchant Category Code
        _tlv('53', '986') +                               # Moeda: Real (BRL)
        _tlv('54', f"{valor:.2f}") +                      # Valor da transação
        _tlv('58', 'BR') +                                # País
        _tlv('59', nome_recebedor) +                      # Nome do recebedor
        _tlv('60', cidade_recebedor) +                    # Cidade do recebedor
        _tlv('62', additional_data)                       # Dados adicionais (identificador)
    )

    payload_com_crc_placeholder = payload + '6304'
    crc = _crc16_ccitt(payload_com_crc_placeholder)

    return payload_com_crc_placeholder + crc


def gerar_qrcode_base64(payload: str) -> str:
    """Gera a imagem do QR Code em base64 (PNG), pronta pra usar em <img src="data:image/png;base64,...">."""
    img = qrcode.make(payload)
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode('utf-8')