import datetime

from library.bootstrap import Constants
from library.interfaces.sql_database import Database, query_result_to_dict
from library.strategy.bread_crumbs import BreadCrumb


class DataLoader:
    VALUE_DATA_TYPES = ['valuation']

    def __init__(self, data_type, db_name=None):
        self._db = Database(Constants.db_path, Constants.environment, name=db_name)
        self.type = data_type
        self.data = {}
        self.warnings = {}

    def report_warnings(self):
        if Constants.log:
            log_prefix = 'Data Loader: '
            if self.warnings:
                for data_type in self.warnings:
                    data_warnings = self.warnings[data_type]
                    Constants.log.warning('{}Data warning: type: {}, {}, '.format(log_prefix, data_type, data_warnings))
            else:
                Constants.log.info('{}No data warnings.'.format(log_prefix))


# Expect issues with this.
class BreadCrumbsDataLoader(DataLoader):
    BREAD_CRUMBS_TIME_SERIES = 'bread_crumbs_time_series'

    def __init__(self):
        DataLoader.__init__(self, self.BREAD_CRUMBS_TIME_SERIES)

    def load_bread_crumbs_time_series(self, strategy_name):
        self.data[self.type] = {strategy_name: {}}
        bread_crumb_rows = self._db.query_table(BreadCrumb.TABLE, 'strategy="{}"'.format(strategy_name))
        if bread_crumb_rows:
            # TODO need to refactor according to new design.
            # Extract time series.
            bread_crumb_time_series = [(bread_crumb_row[-3:]) for bread_crumb_row in bread_crumb_rows]

            # Group time series by type.
            way_point_types = set([w[0] for w in bread_crumb_time_series])
            for bread_crumb_type in way_point_types:
                data = [[w[1], w[2]] for w in bread_crumb_time_series if w[0] == bread_crumb_type]
                self.data[self.type][strategy_name][bread_crumb_type] = data
        else:
            self.warnings[self.type] = {strategy_name: 'not_in_database'}


class MarketDataLoader(DataLoader):
    DB_NAME = 'market_data'
    TICKER = 'ticker'
    LATEST_TICKER = 'latest_ticker'

    def __init__(self):
        DataLoader.__init__(self, MarketDataLoader.TICKER, db_name=MarketDataLoader.DB_NAME)

    @staticmethod
    def _staleness(time_series, scope=1):
        # TODO this is shit
        # Quantifies staleness
        #   time_series [(datetime, float)]
        #   Will return True if any value is the same as the next *scope* elements including itself.
        #   e.g. scope = 3,  [..., 91.61, |91.65, 91.65, 91.64|, 91.64, ...] => False

        values = [t[1] for t in time_series]
        scope = int(scope)
        stale_count = 0
        for i in range(0, len(values)):
            values_in_scope = [values[i + j] for j in range(scope)] if i + (scope - 1) < len(values) else values[i:]
            if len(values_in_scope) > 1 and (len(set(values_in_scope)) != len(values_in_scope)):
                stale_count += 1
        return stale_count / len(time_series)

    def _load_ticks(self, symbol, before, after, stale_scope=None):
        # Read ticks from database.
        condition = 'symbol="{0}" AND date_time<"{1}" AND date_time>"{2}"'.format(symbol, before, after)
        tick_rows = self._db.query_table('ticks', condition)

        # Read required data into time series [(datetime, float)].
        ticks_time_series = []
        warnings = []
        for tick_row in tick_rows:
            tick_dict = query_result_to_dict([tick_row], Constants.configs['tables'][MarketDataLoader.DB_NAME]['ticks'])[0]
            tick_datetime = datetime.datetime.strptime(tick_dict['date_time'], Constants.DATETIME_FORMAT)
            tick_value = float(tick_dict['price'])
            tick_volume = int(tick_dict['volume'])
            ticks_time_series.append((tick_datetime, tick_value, tick_volume))

            # Carry out any data checks.
            if stale_scope and self._staleness(ticks_time_series, scope=int(stale_scope)):
                warnings.append('stale_ticker_{0}'.format(symbol.lower()))

        # Return data.
        return ticks_time_series, warnings

    def load_tickers(self, symbol, before, after):
        before = datetime.datetime.strftime(before, Constants.DATETIME_FORMAT)
        after = datetime.datetime.strftime(after, Constants.DATETIME_FORMAT)
        data, warnings = self._load_ticks(symbol, before, after)
        if data:
            if self.type in self.data:
                self.data[self.type][symbol] = data
            else:
                self.data[self.type] = {symbol: data}
        if warnings:
            if self.type in self.warnings:
                self.warnings[self.type][symbol] = warnings
            else:
                self.warnings[self.type] = {symbol: warnings}

    def load_latest_ticker(self, symbol, now=None):
        self.type = MarketDataLoader.LATEST_TICKER
        now = now if now else datetime.datetime.now()
        now_datetime_string = now.strftime(Constants.DATETIME_FORMAT)

        # Read tick from database.
        condition = 'symbol="{0}" AND date_time<{1}'.format(symbol, now_datetime_string)
        tick_rows = self._db.get_one_row('ticks', condition, columns='max(date_time), value')
        if tick_rows[1]:
            self.data[self.type] = {symbol: float(tick_rows[1])}
        else:
            self.warnings[self.type] = {symbol: 'no_ticks_{0}'.format(symbol.lower())}



