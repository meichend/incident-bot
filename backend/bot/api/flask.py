import config
import json
import logging
import logging.config

from bot.shared import tools
from datetime import timedelta
from flasgger import Swagger
from flask import Flask, request, Response
from flask import jsonify, make_response, redirect, render_template
from flask_cors import CORS
from flask_debugtoolbar import DebugToolbarExtension
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_marshmallow import Marshmallow
from flask import Flask

logger = logging.getLogger("api")

"""
Run Init Tasks
"""

live_api_route = "/api/v1"

app = Flask(
    __name__, static_folder="../../app/static", template_folder="../../app"
)
ma = Marshmallow(app)
swagger = Swagger(app)

cors = CORS(
    app, supports_credentials=True, resources={r"/api/*": {"origins": "*"}}
)

app.config["CORS_HEADERS"] = "Content-Type"
app.config["JWT_SECRET_KEY"] = config.jwt_secret_key
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=8)
app.config["SECRET_KEY"] = config.flask_app_secret_key
app.config["SWAGGER"] = {
    "title": "Incident Bot API",
}

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
)

app.debug = config.flask_debug_mode
toolbar = DebugToolbarExtension(app)

"""
Request Modifiers
"""


@app.before_request
def basic_authentication():
    if request.method.lower() == "options":
        return Response()


@app.after_request
def webserver_logging(response):
    logger.info(
        '{} {} [{}] "{} {}" - {} - {} {}'.format(
            request.headers["host"],
            request.access_route[-1],
            tools.fetch_timestamp(short=True),
            request.method,
            request.path,
            request.headers["user_agent"],
            request.environ.get("SERVER_PROTOCOL"),
            response.status,
        )
    )
    return response


"""
Base Routes
"""


@app.route("/")
def redirect_root():
    return redirect("/app")


@app.route("/app", defaults={"path": ""})
@app.route("/app/<path:path>")
def catch_all(path):
    return render_template("index.html")


"""
Error Handlers
"""


@app.errorhandler(404)
def not_found_handler(e):
    return make_response(jsonify({"error": "Not found"}), 404)


@app.errorhandler(429)
def ratelimit_handler(e):
    return (
        json.dumps(
            {
                "success": False,
                "error": "ratelimit exceeded: {}".format(e.description),
            }
        ),
        429,
        {"ContentType": "application/json"},
    )


"""
API Route Definitions
"""

from .routes.auth import auth
from .routes.health import health_check
from .routes.incident import incidentrt
from .routes.job import job
from .routes.pager import pager
from .routes.setting import setting
from .routes.user import user

app.register_blueprint(auth, url_prefix=live_api_route)
app.register_blueprint(health_check, url_prefix=live_api_route)
app.register_blueprint(incidentrt, url_prefix=live_api_route)
app.register_blueprint(job, url_prefix=live_api_route)
app.register_blueprint(pager, url_prefix=live_api_route)
app.register_blueprint(setting, url_prefix=live_api_route)
app.register_blueprint(user, url_prefix=live_api_route)
