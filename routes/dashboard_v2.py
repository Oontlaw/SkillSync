from flask import Blueprint

from routes.dashboard import (
    dashboard_live,
    dashboard_ml_anomaly_feedback,
    dashboard_ml_burnout_feedback,
    dashboard_ml_federated_train,
    dashboard_ml_retrain,
    index as dashboard_index,
)


dashboard_v2_bp = Blueprint('dashboard_v2', __name__)


@dashboard_v2_bp.route('/v2/')
def index():
    return dashboard_index('dashboard_v2.html')


@dashboard_v2_bp.route('/v2/_live')
def dashboard_v2_live():
    return dashboard_live()


@dashboard_v2_bp.route('/v2/ml/anomaly-feedback', methods=['POST'])
def dashboard_v2_anomaly_feedback():
    return dashboard_ml_anomaly_feedback()


@dashboard_v2_bp.route('/v2/ml/burnout-feedback', methods=['POST'])
def dashboard_v2_burnout_feedback():
    return dashboard_ml_burnout_feedback()


@dashboard_v2_bp.route('/v2/ml/retrain', methods=['POST'])
def dashboard_v2_retrain():
    return dashboard_ml_retrain()


@dashboard_v2_bp.route('/v2/ml/federated-train', methods=['POST'])
def dashboard_v2_federated_train():
    return dashboard_ml_federated_train()
