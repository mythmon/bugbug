# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import logging
import lzma
import os
import shutil
from urllib.request import urlretrieve

import requests
from redis import Redis

from bugbug import bugzilla
from bugbug.models.component import ComponentModel
from bugbug.models.defect_enhancement_task import DefectEnhancementTaskModel
from bugbug.models.regression import RegressionModel

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger()

MODELS = {
    "defectenhancementtask": DefectEnhancementTaskModel,
    "component": ComponentModel,
    "regression": RegressionModel,
}
MODELS_NAMES = MODELS.keys()
MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
BASE_URL = "https://index.taskcluster.net/v1/task/project.releng.services.project.testing.bugbug_train.latest/artifacts/public"


def load_model(model):
    model_file_path = os.path.join(MODELS_DIR, f"{model}model")
    LOGGER.info(f"Lookup model in {model_file_path}")
    model = MODELS[model].load(model_file_path)
    return model


def retrieve_model(name):
    os.makedirs(MODELS_DIR, exist_ok=True)

    file_name = f"{name}model"
    file_path = os.path.join(MODELS_DIR, file_name)

    model_url = f"{BASE_URL}/{file_name}.xz"
    LOGGER.info(f"Checking ETAG of {model_url}")
    r = requests.head(model_url, allow_redirects=True)
    r.raise_for_status()
    new_etag = r.headers["ETag"]

    try:
        with open(f"{file_path}.etag", "r") as f:
            old_etag = f.read()
    except IOError:
        old_etag = None

    if old_etag != new_etag:
        LOGGER.info(f"Downloading the model from {model_url}")
        urlretrieve(model_url, f"{file_path}.xz")

        with lzma.open(f"{file_path}.xz", "rb") as input_f:
            with open(file_path, "wb") as output_f:
                shutil.copyfileobj(input_f, output_f)
                LOGGER.info(f"Written model in {file_path}")

        with open(f"{file_path}.etag", "w") as f:
            f.write(new_etag)
    else:
        LOGGER.info(f"ETAG for {model_url} is ok")

    return file_path


def classify_bug(model_name, bug_id, bugzilla_token, expiration=500):
    # This should be called in a process worker so it should be safe to set
    # the token here
    bugzilla.set_token(bugzilla_token)
    bugs = bugzilla._download(bug_id)
    redis_key = f"result_{model_name}_{bug_id}"

    # TODO: Put redis address in env
    redis = Redis(host="localhost")

    if not bugs:
        print("Couldn't get the bug back!")
        # TODO: Find a better error format
        encoded_data = json.dumps({"available": False})

        redis.set(redis_key, encoded_data)
        redis.expire(redis_key, expiration)

        return "OK"

    model = load_model(model_name)  # TODO: Cache the model in the process memory
    probs = model.classify(list(bugs.values()), True)
    indexes = probs.argmax(axis=-1)
    suggestions = model.clf._le.inverse_transform(indexes)

    data = {
        "probs": probs.tolist()[0],  # Redis-py doesn't like a list
        "indexes": indexes.tolist()[0],  # Redis-py doesn't like a list
        "suggestions": suggestions.tolist()[0],  # Redis-py doesn't like a list
    }

    encoded_data = json.dumps(data)

    redis.set(redis_key, encoded_data)
    redis.expire(redis_key, expiration)

    return "OK"
