from flask import Flask
from momo_api import momo_bp

app = Flask(__name__)
app.register_blueprint(momo_bp)

if __name__ == '__main__':
    app.run()
