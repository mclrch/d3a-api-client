import os
import requests
import json
import logging
import uuid
from functools import wraps
from d3a_interface.utils import key_in_dict_and_not_none, get_area_name_uuid_mapping
import ast
from d3a_interface.utils import RepeatingTimer
from d3a_interface.constants_limits import JWT_TOKEN_EXPIRY_IN_SECS

logger = logging.getLogger(__name__)


class AreaNotFoundException(Exception):
    pass


class RedisAPIException(Exception):
    pass


class RestCommunicationMixin:

    def _create_jwt_refresh_timer(self, sim_api_domain_name):
        self.jwt_refresh_timer = RepeatingTimer(
            JWT_TOKEN_EXPIRY_IN_SECS - 30, self._refresh_jwt_token, [sim_api_domain_name]
        )
        self.jwt_refresh_timer.daemon = True
        self.jwt_refresh_timer.start()

    def _refresh_jwt_token(self, domain_name):
        self.jwt_token = retrieve_jwt_key_from_server(domain_name)

    @property
    def _url_prefix(self):
        return f'{self.domain_name}/external-connection/api/{self.simulation_id}/{self.device_id}'

    def _post_request(self, endpoint_suffix, data):
        endpoint = f"{self._url_prefix}/{endpoint_suffix}/"
        data["transaction_id"] = str(uuid.uuid4())
        return data["transaction_id"], post_request(endpoint, data, self.jwt_token)

    def _get_request(self, endpoint_suffix, data):
        endpoint = f"{self._url_prefix}/{endpoint_suffix}/"
        data["transaction_id"] = str(uuid.uuid4())
        return data["transaction_id"], get_request(endpoint, data, self.jwt_token)


def retrieve_jwt_key_from_server(domain_name):
    resp = requests.post(
        f"{domain_name}/api-token-auth/",
        data=json.dumps({"username": os.environ["API_CLIENT_USERNAME"],
                         "password": os.environ["API_CLIENT_PASSWORD"]}),
        headers={"Content-Type": "application/json"})
    if resp.status_code != 200:
        logger.error(f"Request for token authentication failed with status code {resp.status_code}. "
                     f"Response body: {resp.text}")
        return
    return json.loads(resp.text)["token"]


def post_request(endpoint, data, jwt_token):
    resp = requests.post(
        endpoint,
        data=json.dumps(data),
        headers={"Content-Type": "application/json",
                 "Authorization": f"JWT {jwt_token}"})
    return json.loads(resp.text) if request_response_returns_http_2xx(endpoint, resp) else None


def blocking_post_request(endpoint, data, jwt_token):
    data["transaction_id"] = str(uuid.uuid4())
    resp = requests.post(
        endpoint,
        data=json.dumps(data),
        headers={"Content-Type": "application/json",
                 "Authorization": f"JWT {jwt_token}"})
    return json.loads(resp.text) if request_response_returns_http_2xx(endpoint, resp) else None


def get_request(endpoint, data, jwt_token):
    resp = requests.get(
        endpoint,
        data=json.dumps(data),
        headers={"Content-Type": "application/json",
                 "Authorization": f"JWT {jwt_token}"})
    return request_response_returns_http_2xx(endpoint, resp)


def request_response_returns_http_2xx(endpoint, resp):
    if 200 <= resp.status_code <= 299:
        return True
    else:
        logger.error(f"Request to {endpoint} failed with status code {resp.status_code}. "
                     f"Response body: {resp.text}")
        return False


def get_aggregator_prefix(domain_name, simulation_id):
    return f"{domain_name}/external-connection/aggregator-api/{simulation_id}/"


def blocking_get_request(endpoint, data, jwt_token):
    data["transaction_id"] = str(uuid.uuid4())
    resp = requests.get(
        endpoint,
        data=json.dumps(data),
        headers={"Content-Type": "application/json",
                 "Authorization": f"JWT {jwt_token}"})
    return json.loads(resp.json()) if request_response_returns_http_2xx(endpoint, resp) else None


def get_area_uuid_from_area_name(serialized_scenario, area_name):
    if "name" in serialized_scenario and serialized_scenario["name"] == area_name:
        return serialized_scenario["uuid"]
    if "children" in serialized_scenario:
        for child in serialized_scenario["children"]:
            area_uuid = get_area_uuid_from_area_name(child, area_name)
            if area_uuid is not None:
                return area_uuid
    return None


def get_area_uuid_from_area_name_and_collaboration_id(collab_id, area_name, domain_name):
    jwt_key = retrieve_jwt_key_from_server(domain_name)
    if jwt_key is None:
        return
    from sgqlc.endpoint.http import HTTPEndpoint

    url = f"{domain_name}/graphql/"
    headers = {'Authorization': f'JWT {jwt_key}', 'Content-Type': 'application/json'}

    query = 'query { readConfiguration(uuid: "{' + collab_id + \
            '}") { scenarioData { latest { serialized } } } }'

    endpoint = HTTPEndpoint(url, headers)
    data = endpoint(query=query)
    area_uuid = get_area_uuid_from_area_name(
        json.loads(data["data"]["readConfiguration"]["scenarioData"]["latest"]["serialized"]), area_name
    )
    if not area_uuid:
        raise AreaNotFoundException(f"Area with name {area_name} is not part of the "
                                    f"collaboration with UUID {collab_id}")
    return area_uuid


def get_area_uuid_and_name_mapping_from_simulation_id(collab_id, domain_name):
    jwt_key = retrieve_jwt_key_from_server(domain_name)
    from sgqlc.endpoint.http import HTTPEndpoint

    url = f"{domain_name}/graphql/"
    headers = {'Authorization': f'JWT {jwt_key}', 'Content-Type': 'application/json'}

    query = 'query { readConfiguration(uuid: "{' + collab_id + \
            '}") { scenarioData { latest { serialized } } } }'

    endpoint = HTTPEndpoint(url, headers)
    data = endpoint(query=query)
    if key_in_dict_and_not_none(data, 'errors'):
        return ast.literal_eval(data['errors'][0]['message'])
    else:
        area_name_uuid_map = get_area_name_uuid_mapping(
            json.loads(data["data"]["readConfiguration"]["scenarioData"]["latest"]["serialized"])
        )
        return area_name_uuid_map


def logging_decorator(command_name):
    def decorator(f):
        @wraps(f)
        def wrapped(self, *args, **kwargs):
            logger.debug(f'Sending command {command_name} to device.')
            return_value = f(self, *args, **kwargs)
            logger.debug(f'Command {command_name} responded with: {return_value}.')
            return return_value
        return wrapped
    return decorator


def list_running_canary_networks_and_devices_with_live_data(domain_name):
    jwt_key = retrieve_jwt_key_from_server(domain_name)
    if jwt_key is None:
        return
    from sgqlc.endpoint.http import HTTPEndpoint

    url = f"{domain_name}/graphql/"
    headers = {'Authorization': f'JWT {jwt_key}', 'Content-Type': 'application/json'}

    query = '''
    query {
      listCanaryNetworks {
        configurations {
          uuid
          resultsStatus
          scenarioData { 
            forecastStreamAreaMapping 
          }
        }
      }
    }
    '''

    endpoint = HTTPEndpoint(url, headers)
    data = endpoint(query=query)

    logging.debug(f"Received Canary Network data: {data}")

    return {
        cn["uuid"]: cn["scenarioData"]["forecastStreamAreaMapping"]
        for cn in data["data"]["listCanaryNetworks"]["configurations"]
        if cn["resultsStatus"] == "running"
    }