import datetime
import json
import optparse
import os
import sys

from flask import Flask, request

from library.bootstrap import Constants
from library.data_loader import MarketDataLoader, WayPointDataLoader
from library.interfaces.exchange import AlpacaInterface
from library.interfaces.sql_database import Database, query_result_to_dict
from library.portfolio import Portfolio
from library.utilities.file import parse_configs_file
from library.utilities.job import is_script_new, Job

app = Flask(__name__)


def float_to_string(value_float):
    return '{:,.2f}'.format(value_float)


def response(status, data=None):
    if data:
        return app.response_class(response=json.dumps(data), status=status, mimetype='application/json')
    return app.response_class(status=status, mimetype='application/json')


@app.route('/exchange_open')
def exchange_open():
    # Return "True" if exchange is open and "False" if it is closed.
    exchange = AlpacaInterface(Constants.configs['API_ID'], Constants.configs['API_SECRET_KEY'], simulator=True)
    return response(200, str(exchange.is_exchange_open()))


@app.route('/market_data')
def market_data():
    # Returns a time series data for a ticker with provided symbol and within a given datetime range.
    # Cannot return as dict as element order is not guaranteed.
    # TODO Offer multiple tickers using data loader.

    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'symbol' in params:
        symbol = params['symbol'].upper()

    if 'before' in params:
        before = params['before']
    else:
        before = datetime.datetime.now()

    if 'after' in params:
        after = params['after']
    else:
        after = datetime.datetime.now() - datetime.timedelta(hours=24)

    # Return data.
    data_loader = MarketDataLoader()
    data_loader.load_tickers(symbol, before, after)
    if MarketDataLoader.TICKER in data_loader.data:
        data = data_loader.data[MarketDataLoader.TICKER][symbol]
        data = [(d[0].strftime(Constants.pp_time_format), float_to_string(d[1])) for d in data]
        return response(200, data)
    else:
        return response(401, 'Market data not available.')


@app.route('/strategies')
def strategies():
    # Returns strategy row as dict, will return for all strategies for for one specified by provided strategy_id

    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Initiate database connection.
    db = Database()

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'id' in params:
        # Get portfolio data.
        strategy_row = db.get_one_row('strategies', 'id="{0}"'.format(params['id']))
        if strategy_row is None:
            return response(400, 'Strategy does not exist.')
        strategy_dict = query_result_to_dict([strategy_row], Constants.configs['tables'][Constants.db_name]['strategies'])
        return response(200, strategy_dict)

    all_rows = db.query_table('strategies')
    all_rows_as_dict = query_result_to_dict(all_rows, Constants.configs['tables']['algo_trading_platform']['strategies'])
    return response(200, all_rows_as_dict)


@app.route('/strategy_way_points')
def strategy_way_points():
    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'id' in params:
        way_point_data_loader = WayPointDataLoader()
        way_point_data_loader.load_way_point_time_series(params['id'])
        if WayPointDataLoader.WAY_POINT_TIME_SERIES in way_point_data_loader.data:
            data = way_point_data_loader.data[WayPointDataLoader.WAY_POINT_TIME_SERIES][params['id']]
            return response(200, data)
        else:
            return response(401, 'No way point data found.')

    return response(401, 'Strategy id required.')


# TODO use portfolio class.
@app.route('/portfolio')
def portfolio():
    # Returns portfolio row as dictionary for provided portfolio id.

    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Initiate database connection.
    db = Database()

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'id' in params:
        # Get portfolio data.
        portfolio_row = db.get_one_row('portfolios', 'id="{0}"'.format(params['id']))
        if portfolio_row is None:
            return response(400, 'Portfolio does not exist.')

        portfolio_obj = Portfolio(params['id'], db)

        # Fetch historical valuations.
        condition = 'portfolio_id="{0}"'.format(portfolio_obj.id)
        historical_valuations_rows = db.query_table('historical_portfolio_valuations', condition)
        historical_date_times = [r[2] for r in historical_valuations_rows]
        historical_values = [float(r[3]) for r in historical_valuations_rows]

        # Calculate 24hr PnL.
        twenty_four_hrs_ago = datetime.datetime.now() - datetime.timedelta(hours=24)
        valuations = []
        for row in historical_valuations_rows:
            if datetime.datetime.strptime(row[2], Constants.date_time_format) > twenty_four_hrs_ago:
                valuations.append([row[2], row[3]])

        if valuations:
            pnl = float_to_string(float(valuations[-1][1]) - float(valuations[0][1]))
        else:
            pnl = '-'

        data = {
            'id': portfolio_obj.id,
            'cash': float_to_string(portfolio_obj.cash),
            'value': float_to_string(portfolio_obj.valuate()),
            'historical_valuations': [historical_date_times, historical_values],
            'pnl': pnl
        }

        return response(200, data)

    return response(401, 'Portfolio id required.')


@app.route('/portfolio/assets')
def assets():
    # Returns asset rows as a dictionary for a given portfolio id.

    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'id' in params:
        db = Database()
        exchange = AlpacaInterface(Constants.configs['API_ID'], Constants.configs['API_SECRET_KEY'], simulator=True)
        portfolio_obj = Portfolio(params['id'], db)
        portfolio_obj.sync_with_exchange(exchange)
        for asset in portfolio_obj.assets:
            exposure_as_string = float_to_string(portfolio_obj.assets[asset][Portfolio.EXPOSURE])
            portfolio_obj.assets[asset][Portfolio.EXPOSURE] = exposure_as_string
        return response(200, portfolio_obj.assets)

    return response(401, 'Portfolio id required.')


@app.route('/job')
def job():
    # Returns job row as dictionary for provided job id.

    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'id' in params:
        job_obj = Job(job_id=params['id'])
        start_time_datetime = datetime.datetime.strptime(job_obj.start_time, Constants.date_time_format)

        data = {
            'name': job_obj.name,
            'script': job_obj.script,
            'log_path': job_obj.log_path,
            'start_time': start_time_datetime.strftime(Constants.pp_time_format),
            'elapsed_time': job_obj.elapsed_time,
            'finish_state': job_obj.STATUS_MAP[int(job_obj.finish_state)],
            'version': '{0}{1}'.format(job_obj.version, ' (NEW)' if is_script_new(job_obj.script) else ''),
            'phase_name': job_obj.phase_name
        }

        return response(200, data)

    return response(401, 'Job id required.')


@app.route('/job/log')
def log():
    # Returns log text for provided job id.

    # Authenticate.
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    if client_ip not in Constants.configs['authorised_ip_address']:
        return response(401, 'Client is not authorised.')

    # Initiate database connection.
    db = Database()

    # Extract any parameters from url.
    params = {x: request.args[x] for x in request.args if x is not None}

    if 'id' in params:
        job_row = db.get_one_row('jobs', 'id="{0}"'.format(params['id']))
        if job_row is None:
            return response(401, 'Job doesnt exist.')
        job_dict = query_result_to_dict([job_row], Constants.configs['tables'][Constants.db_name]['jobs'])[0]
        with open(job_dict['log_path'], 'r') as file:
            data = file.read().replace('\n', '<br>')
        return response(200, data)

    return response(401, 'Job id required.')


def parse_cmdline_args(app_name):
    parser = optparse.OptionParser()
    parser.add_option('-e', '--environment', dest="environment")
    parser.add_option('-r', '--root_path', dest="root_path")
    parser.add_option('-j', '--job_name', dest="job_name", default=None)

    options, args = parser.parse_args()
    return parse_configs_file({
        "app_name": app_name,
        "environment": options.environment.lower(),
        "root_path": options.root_path,
        "job_name": options.job_name,
        "script_name": str(os.path.basename(sys.argv[0])).split('.')[0],
    })


if __name__ == '__main__':
    # Setup configs.
    Constants.configs = parse_cmdline_args('algo_trading_platform')

    app.run('0.0.0.0')
    app.run()
