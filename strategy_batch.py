import sys
import datetime
import optparse
import os

import strategies.strategy_methods as strategy_methods
from library.db_interface import Database
from library.data_source_utils import TickerDataSource
from library.file_utils import parse_configs_file
from library.log_utils import get_log_file_path, setup_log, log_configs, log_hr
from library.job_utils import Job


# TODO Implement Exchange.
class Exchange:

    def __init__(self):
        pass

    def ask(self, symbol, units, target_value):
        executed_trade = self._execute_trade('ask', symbol, units, target_value)
        monies_returned = units * self.get_current_ask_price(symbol)
        return executed_trade, monies_returned

    def bid(self, symbol, units, target_value):
        executed_trade = self._execute_trade('bid', symbol, units, target_value)
        cost = units * self.get_current_ask_price(symbol)
        return executed_trade, cost


class ExchangeSimulator(Exchange):

    def __init__(self, db, out_file_path=None):
        Exchange.__init__(self)
        self._out_file_path = out_file_path
        # TODO decide how to implement commissions.
        self.commission = 0.1

    def _execute_trade(self, trade_type, symbol, units, target_value):
        # Write trade to CSV path if set.
        if self._out_file_path:
            with open(self._out_file_path, 'a') as trade_file:
                count = 0
                while count < units:
                    line = '{0},{1},{2}\n'.format(trade_type, symbol, target_value)
                    trade_file.write(line)
                    count += 1
        return trade_type, symbol, units, target_value

    # TODO make this a bit smarter. Probs can get actual values.
    def get_liquidity(self, symbol):
        # return self.api.get_liquidity(symbol)
        return 2000.0

    # TODO make this a bit smarter. Probs can get actual values.
    def get_current_ask_price(self, symbol):
        return float(14.83)





# TODO Implement ExchangeInterface.
class ExchangeInterface(Exchange):

    def __init__(self, db):
        Exchange.__init__(self, db)


class TradeExecutor:

    def __init__(self, db, portfolio_name, exchange):
        self._db = db

        # Read portfolio details from database.
        pf_id, pf_name, exchange_name, capital = db.get_one_row('portfolios', 'name="{0}"'.format(portfolio_name))
        results = db.query_table('assets', 'portfolio_id="{0}"'.format(pf_id))
        self.portfolio = {"assets": {r[2]: int(r[3]) for r in results},
                          "capital": float(capital)}
        # This is the passed exchange object, currently has nothing to do with exchange_name.
        self.exchange = exchange

    def _value_position(self, symbol):
        units = float(self.portfolio['assets'][symbol])
        current_price = float(self.exchange.get_current_ask_price(symbol))
        return units * current_price

    def _calculate_exposure(self, symbol):
        # Assume exposure == maximum possible loss from current position.
        return self._value_position(symbol)

    def _meets_risk_profile(self, strategy, proposed_trade, risk_profile):
        strategy_risk_profile = risk_profile[strategy]
        signal, symbol, no_of_units, target_price = proposed_trade
        if 'max_exposure' in strategy_risk_profile:
            if signal == 'buy':
                potential_exposure = self._calculate_exposure(symbol) + (no_of_units * target_price)
            else:
                potential_exposure = self._calculate_exposure(symbol) - (no_of_units * target_price)
            if potential_exposure > strategy_risk_profile['max_exposure']:
                return False
        if 'min_liquidity' in risk_profile:
            if self.exchange.get_liquidity(symbol) < strategy_risk_profile['min_liquidity']:
                return False
        return True

    def value_portfolio(self):
        value = self.portfolio['capital']
        for asset in self.portfolio['assets']:
            value += self._value_position(asset)
        return value

    def propose_trades(self, strategies_list, signals, risk_profile):
        trades = []
        for strategy in strategies_list:
            # Get required data from database.
            strategy_symbol = self._db.get_one_row('strategies', 'name="{0}"'.format(strategy))[3]
            signal = [s for s in signals if s.symbol == strategy_symbol][0]

            # Calculate number of units for trade.
            units = 1

            # Create trade tuple.
            trade = (signal.signal, signal.symbol, units, signal.target_value)

            # Check trade is valid.
            if self._meets_risk_profile(strategy, trade, risk_profile):
                # Check symbol is in portfolio.
                if signal.symbol not in self.portfolio['assets']:
                    raise Exception('Asset "{0}" not found in portfolio.'.format(signal.symbol))

                # Check portfolio has sufficent capital.
                if signal.signal == 'buy':
                    required_capital = units * signal.target_value
                    if required_capital > float(self.portfolio['capital']):
                        raise Exception('Required capital has exceeded limits.')
                trades.append(trade)
        return trades

    def update_position(self):
        # self.portfolio
        # self._db
        pass

    def execute_trades(self, requested_trades):
        # Return actual achieved trades, Not all trades will be fulfilled.
        executed_trades = []
        for trade in requested_trades:
            signal, symbol, units, target_value = trade
            if signal == 'sell':
                executed_trade, monies_returned = self.exchange.ask(symbol, units, target_value)
                updated_units = self.portfolio['assets'][executed_trade[1]] - executed_trade[2]
                self.portfolio['capital'] += monies_returned
            if signal == 'buy':
                executed_trade, cost = self.exchange.bid(symbol, units, target_value)
                updated_units = self.portfolio['assets'][executed_trade[1]] + executed_trade[2]
                self.portfolio['capital'] -= cost
            self.portfolio['assets'][executed_trade[1]] = updated_units
            executed_trades.append(executed_trade)
        self.update_position()
        return executed_trades


class SignalGenerator:

    def __init__(self, db, log, run_date, run_time, ds=None):
        self._db = db
        self._ds = ds if ds else None
        self._log = log
        self._run_date = run_date
        self._run_time = run_time

    def _evaluate_strategy(self, strategy_name):
        # Get strategy function and arguments.
        context = StrategyContext(self._db, self._run_date, self._run_time, self._ds)
        strategy_row = self._db.get_one_row('strategies', 'name="{0}"'.format(strategy_name))
        args = strategy_row[3]
        method = strategy_row[4]

        # Check method exists.
        if method not in dir(strategy_methods):
            raise Exception('Strategy function "{0}" not found.'.format(method))

        # Prepare function call.
        args = ['"{0}"'.format(a) for a in args.split(',')] if args else ''
        args_str = ','.join(args)
        try:
            signal = eval('strategy_methods.{0}(context,{1})'.format(method, args_str))
        except Exception as error:
            signal = error

        # Save signals to db or handle strategy errors.
        if not signal:
            self._log.error('Error evaluating strategy "{0}": {1}'.format(strategy_name, signal))

        return signal

    def evaluate_strategies(self, strategies_list):
        # Evaluate strategies.
        #   Might want to evaluate concurrently?
        signals = [self._evaluate_strategy(s) for s in strategies_list]
        return signals


class StrategyContext:

    def __init__(self, db, run_date, run_time, ds=None):
        now = datetime.datetime.now()
        run_date = run_date if run_date else now.strftime('%Y%m%d')
        run_time = run_time if run_time else now.strftime('%H%M%S')
        self.now = datetime.datetime.strptime(run_date + run_time, '%Y%m%d%H%M%S')
        self.db = db
        self.ds = ds if ds else None
        self.signal = Signal(0)


class Signal:

    def __init__(self, signal_id):
        self.id = signal_id
        self.symbol = None
        self.signal = None
        # "target" because can always sell for more or buy for less I assume.
        self.target_value = None
        self.datetime = datetime.datetime.now()

    def __str__(self):
        target_value_pp = '' if self.signal == 'hold' else ' @ {0}'.format(str(self.target_value))
        return '[{0} {1}{2}]'.format(self.signal, self.symbol, target_value_pp)

    def __repr__(self):
        return self.__str__()

    def sell(self, symbol, price):
        self.symbol = symbol
        self.signal = 'sell'
        self.target_value = price

    def buy(self, symbol, price):
        self.symbol = symbol
        self.signal = 'buy'
        self.target_value = price

    def hold(self, symbol):
        self.symbol = symbol
        self.signal = 'hold'
        self.target_value = None

    # TODO Implement Signal save_to_db.
    def save_to_db(self, db, log):
        if log:
            log.info('Saved signal: {}'.format(self.__str__()))


def clean_signals(dirty_signals):
    # Remove errors.
    signals = [ds for ds in dirty_signals if isinstance(ds, Signal)]

    # Group symbols by symbol.
    unique_symbols = list(set([s.symbol for s in signals]))
    signals_per_unique_symbol = {us: [s for s in signals if s.symbol == us] for us in unique_symbols}

    for symbol in unique_symbols:
        symbol_signals = [s.signal for s in signals_per_unique_symbol[symbol]]
        symbol_signals_set = list(set(symbol_signals))

        # If all the signals agree unify signal per symbol, else raise error for symbol (maybe allow manual override)
        unanimous_signal = None if len(symbol_signals_set) > 1 else symbol_signals_set[0]
        if unanimous_signal:
            target_values = [s.target_value for s in signals_per_unique_symbol[symbol]]
            if unanimous_signal == 'buy':
                # Buy for cheapest ask.
                final_signal_index = target_values.index(min(target_values))
            elif unanimous_signal == 'sell':
                # Sell to highest bid.
                final_signal_index = target_values.index(max(target_values))
            else:
                final_signal_index = 0
            signals_per_unique_symbol[symbol] = signals_per_unique_symbol[symbol][final_signal_index]
        else:
            conflicting_signals_str = ', '.join([str(s) for s in signals_per_unique_symbol[symbol]])
            raise Exception('Could not unify conflicting signals for "{0}": {1}'.format(symbol, conflicting_signals_str))

    # Return cleaned signals.
    return [signals_per_unique_symbol[signal] for signal in unique_symbols]


def generate_risk_profile(db, strategies_list, risk_appetite=1.0):
    # Returns risk profile, dict of factor: values.
    risk_profile = {}
    for strategy in strategies_list:
        # Get risk profile for strategy.
        condition = 'name="{0}"'.format(strategy)
        risk_profile_id = db.get_one_row('strategies', condition)[2]
        condition = 'id="{0}"'.format(risk_profile_id)
        headers = [h[1] for h in db.execute_sql('PRAGMA table_info(risk_profiles);')]
        risk_profile_row = [float(v) for v in db.get_one_row('risk_profiles', condition)]

        # Package risk profile into a dictionary.
        risk_profile_dict = dict(zip(headers[1:], risk_profile_row[1:]))
        for name in risk_profile_dict:
            if 'max' in name:
                risk_profile_dict[name] = risk_profile_dict[name] * risk_appetite
            if 'min' in name:
                if risk_appetite > 1:
                    multiplier = 1 - (risk_appetite - 1)
                else:
                    multiplier = (1 - risk_appetite) + 1
                risk_profile_dict[name] = risk_profile_dict[name] * multiplier

        risk_profile[strategy] = risk_profile_dict
    return risk_profile


def parse_cmdline_args(app_name):
    parser = optparse.OptionParser()
    parser.add_option('-e', '--environment', dest="environment")
    parser.add_option('-r', '--root_path', dest="root_path")
    parser.add_option('-j', '--job_name', dest="job_name", default=None)
    parser.add_option('--dry_run', action="store_true", default=False)

    # Initiate script specific args.
    parser.add_option('-s', '--strategies', dest="strategies")
    parser.add_option('-d', '--data_source', dest="data_source")
    # Specify "simulate" or "execute" modes.
    parser.add_option('-m', '--mode', dest="mode")
    # Can be ran for any date or time, both default to now.
    #   these will help back testing, and can make the run_time precise and remove any lag in cron.
    parser.add_option('--run_date', dest="run_date", default=None)
    parser.add_option('--run_time', dest="run_time", default=None)

    options, args = parser.parse_args()
    return parse_configs_file({
        "app_name": app_name,
        "environment": options.environment.lower(),
        "root_path": options.root_path,
        "job_name": options.job_name,
        "script_name": str(os.path.basename(sys.argv[0])).split('.')[0],
        "dry_run": options.dry_run,

        # Parse script specific args.
        "data_source": options.data_source,
        "strategies": options.strategies.lower(),
        "mode": options.mode,
        "run_date": options.run_date,
        "run_time": options.run_time
    })


def main():
    # Setup configs.
    global configs
    configs = parse_cmdline_args('algo_trading_platform')
    # configs = parse_configs_file(cmdline_args)

    # Setup logging.
    log_path = get_log_file_path(configs['logs_root_path'], configs['script_name'])
    log = setup_log(log_path, True if configs['environment'] == 'dev' else False)
    log_configs(configs, log)

    # Setup database.
    db = Database(configs['db_root_path'], 'algo_trading_platform', configs['environment'])
    db.log(log)

    # Initiate Job
    job = Job(configs, db)
    job.log(log)

    # Setup data source if one is specified in the args.
    ds = TickerDataSource(configs['data_source'], configs['db_root_path'], configs['environment']) if configs['data_source'] else None

    # Evaluate strategies [Signals], just this section can be used to build a strategy function test tool.
    sg = SignalGenerator(db, log, configs['run_date'], configs['run_time'], ds)
    strategies_list = configs['strategies'].split(',')
    signals = sg.evaluate_strategies(strategies_list)

    # Check for conflicting signals [Signals].
    cleaned_signals = clean_signals(signals)
    for signal in cleaned_signals:
        signal.save_to_db(db, log)

    # Calculate risk profile {string(strategy name): float(risk value)}.
    risk_profile = generate_risk_profile(db, strategies_list)

    # Read in portfolio.
    portfolio = None

    # Initiate exchange.
    if configs['mode'] == 'simulate':
        exchange = ExchangeSimulator(db, out_file_path='/Users/joshnicholls/PycharmProjects/algo_trading_platform/drive/trade_requests.csv', )
    elif configs['mode'] == 'execute':
        exchange = ExchangeInterface(db)
    else:
        raise Exception('Mode "{0}" is not valid.'.format(configs['mode']))

    # Initiate trade executor.
    trade_executor = TradeExecutor(db, 'test_portfolio', exchange)
    inital_value = trade_executor.value_portfolio()

    # Prepare trades.
    trades = trade_executor.propose_trades(strategies_list, cleaned_signals, risk_profile)

    # Execute trades.
    executed_trades = trade_executor.execute_trades(trades)

    resulting_value = trade_executor.value_portfolio()
    pnl = resulting_value - inital_value
    log.info('Value: {0}, PnL: {1}'.format(resulting_value, pnl))

    job.finished(log)


if __name__ == "__main__":
    sys.exit(main())