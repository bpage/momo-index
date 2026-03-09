from flask import Flask, send_file
from momo_api import momo_bp

app = Flask(__name__)
app.register_blueprint(momo_bp)

@app.route('/')
def index():
    return send_file('momo-index-v3.html')

if __name__ == '__main__':
    app.run()
