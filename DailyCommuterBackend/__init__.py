import os

from flask import Flask
from flask_cors import CORS


def create_app(test_config=None):
    # create and configure the app
    app = Flask(__name__, instance_relative_config=True, template_folder="templates")

    # Enables CORS for all routes and all origins
    # Maybe change this in the future to only allow specific routs (for the APIs)
    CORS(app)
    
    app.config.from_mapping(
        # SECRET_KEY should be overridden with a random value when deploying
        # Maybe set via config.py below?
        SECRET_KEY='dev',
        DATABASE=os.path.join(app.instance_path, 'DailyCommuter.sqlite'),
    )

    if test_config is None:
        # load the instance config, if it exists, when not testing
        app.config.from_pyfile('config.py', silent=True)
    else:
        # load the test config if passed in
        app.config.from_mapping(test_config)

    # ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    from . import db
    db.init_app(app)

    # Add when implementing users/login
    # from . import auth
    # app.register_blueprint(auth.bp)

    from . import home
    app.register_blueprint(home.bp)
    app.add_url_rule('/', endpoint='index')

    return app
