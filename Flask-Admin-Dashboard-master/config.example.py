# ===================================================================
# ARQUIVO DE EXEMPLO — copie para config.py e preencha com valores reais.
# config.py NUNCA deve ser enviado ao GitHub (já está no .gitignore).
# ===================================================================

# Segurança do Flask / Flask-Security
SECRET_KEY = 'troque-por-uma-chave-aleatoria-longa'
SECURITY_PASSWORD_SALT = 'troque-por-outra-chave-aleatoria-longa'

# Banco de dados
DATABASE_FILE = 'instance/database.sqlite'
SQLALCHEMY_DATABASE_URI = 'sqlite:///database.sqlite'
SQLALCHEMY_TRACK_MODIFICATIONS = False

# === Focus NFe (emissão fiscal) ===
FOCUS_NFE_AMBIENTE = 'homologacao'   # trocar para 'producao' quando for pra valer
FOCUS_NFE_TOKEN = 'seu-token-aqui'
EMPRESA_CNPJ = '00000000000000'

# === Pix ===
PIX_KEY = 'sua-chave-pix-aqui'
PIX_MERCHANT_NAME = 'HD DRONES'
PIX_MERCHANT_CITY = 'SAO PAULO'