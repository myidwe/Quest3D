"""Prewarm explicit local depth choices, then switch on one AI worker only."""
from __future__ import annotations

import threading

from .assets import DEFAULT_MODEL_ID, DEPTH_MODEL_IDS
from .depth import DepthEngine


class SelectableDepthEngine:
    def __init__(self, ai_size, *, model_id=DEFAULT_MODEL_ID,
                 model_ids=(DEFAULT_MODEL_ID,), **engine_options):
        if not isinstance(model_ids, tuple) or not model_ids:
            raise ValueError("model_ids must be a nonempty tuple of distinct supported IDs")
        if any(not isinstance(item, str) or item not in DEPTH_MODEL_IDS for item in model_ids):
            raise ValueError("Unknown prewarmed depth model")
        if len(set(model_ids)) != len(model_ids):
            raise ValueError("model_ids must contain distinct supported IDs")
        if model_id not in model_ids:
            raise ValueError("Selected model is not in model_ids")
        self.model_ids = model_ids
        self._model_id = model_id
        self._engines = {}
        self._prepared_geometry = None
        self._worker_ident = None
        self._lock = threading.RLock()
        self._inflight = False
        self._closing = False
        self._closed = False
        try:
            for item in model_ids:
                self._engines[item] = DepthEngine(ai_size, model_id=item, **engine_options)
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup:
                error.add_note(f"Depth choice startup cleanup: {cleanup}")
            raise

    @property
    def model_id(self):
        return self._model_id

    def _require_open(self):
        if self._closed or self._closing:
            raise RuntimeError("Depth choices are closed or closing")

    def _require_worker(self):
        self._require_open()
        if self._prepared_geometry is None:
            raise RuntimeError("Prepare every depth choice before starting capture or selecting a model")
        ident = threading.get_ident()
        if self._worker_ident is None:
            self._worker_ident = ident
        elif self._worker_ident != ident:
            raise RuntimeError("Depth selection and inference must stay on the same AI worker thread")
        if self._inflight:
            raise RuntimeError("Depth inference is already in progress")

    def prepare_execution(self, source_width, source_height):
        with self._lock:
            self._require_open()
            geometry = (source_width, source_height)
            if self._prepared_geometry is not None:
                if geometry != self._prepared_geometry:
                    raise RuntimeError("Depth choices cannot recapture graphs; reopen for new source geometry")
                return self.execution_status()
            if self._worker_ident is not None:
                raise RuntimeError("Depth choices must be prepared before the AI worker starts")
            try:
                for engine in self._engines.values():
                    engine.prepare_execution(source_width, source_height)
                self._prepared_geometry = geometry
            except BaseException as error:
                try:
                    self.close()
                except BaseException as cleanup:
                    error.add_note(f"Depth choice preparation cleanup: {cleanup}")
                raise
            return self.execution_status()

    def select_model(self, model_id):
        with self._lock:
            if model_id not in self.model_ids:
                raise ValueError("Depth model was not prepared for this session")
            self._require_worker()
            self._model_id = model_id

    def infer(self, *args, **kwargs):
        with self._lock:
            self._require_worker()
            engine = self._engines[self._model_id]
            self._inflight = True
        try:
            return engine.infer(*args, **kwargs)
        finally:
            with self._lock:
                self._inflight = False

    def execution_status(self):
        with self._lock:
            engine = self._engines.get(self._model_id)
            status = dict(engine.execution_status()) if engine is not None else {}
            status.update(model_id=self._model_id, available_models=list(self.model_ids),
                          prewarmed_models=list(self.model_ids) if self._prepared_geometry is not None else [],
                          closed=self._closed)
            return status

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._inflight:
                raise RuntimeError("Stop the AI worker before releasing depth choices")
            self._closing = True
            failures = []
            for item in reversed(tuple(self._engines)):
                try:
                    self._engines[item].close()
                except BaseException as error:
                    failures.append((item, error))
                else:
                    del self._engines[item]
            if failures:
                first = failures[0][1]
                for item, error in failures[1:]:
                    first.add_note(f"Depth choice cleanup for {item}: {error}")
                raise first
            self._closed = True
