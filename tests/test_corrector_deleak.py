"""C3 de-leak regression: the corrector's feature matrix must not encode
the correction target. The old correction_delta feature made
corrected = original + delta recoverable exactly from X, inflating LOO-CV."""
import numpy as np

from database import AdminCorrection, Worker, db


def test_corrector_feature_builder_is_leak_free(app):
    from ml.corrector import _build_training_data

    with app.app_context():
        w = Worker(name="C", email="c@example.com", discord_id="900")
        db.session.add(w)
        db.session.flush()
        for i in range(6):
            db.session.add(
                AdminCorrection(
                    worker_id=w.id,
                    original_score_change=5.0,
                    corrected_score_change=float(i),
                    reason="test",
                    corrected_by="admin",
                )
            )
        db.session.commit()

        X, y_reg, y_cls, corrections, count = _build_training_data()
        assert X is not None and X.shape[1] == 2  # abs(original), past count

        deltas = np.array(
            [c.corrected_score_change - c.original_score_change for c in corrections]
        )
        originals = np.array([c.original_score_change for c in corrections])
        targets = np.array(y_reg)
        for col in range(X.shape[1]):
            # the removed delta feature must not survive under any guise
            assert not np.allclose(X[:, col], deltas)
            # and original + any column must not reconstruct the target
            assert not np.allclose(originals + X[:, col], targets)
