import os
import ccxt
import logging
import threading
import time
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')

if not api_key or not api_secret:
    raise ValueError("API_KEY or API_SECRET not found in .env file")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
socketio = SocketIO(app)
bots = {}

def get_exchange(testnet=False):
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
    })
    if testnet:
        exchange.set_sandbox_mode(True)
        logging.info("Using Binance Testnet")
    else:
        logging.info("Using Binance Live")
    return exchange

class GridTradingBot:
    def __init__(self, exchange, base, quote, grid_size, upper_price, lower_price, investment, max_open_buys):
        self.exchange = exchange
        self.base = base
        self.quote = quote
        self.symbol = base + quote
        self.grid_size = grid_size
        self.upper_price = upper_price
        self.lower_price = lower_price
        self.investment = investment
        self.max_open_buys = max_open_buys
        self.initialized = False
        self.trading = False
        self.running = False
        self.lock = threading.Lock()
        self.initial_balance = None

    def initialize(self):
        try:
            self.initial_balance = self.get_balance()
            self.initialized = True
            logging.info(f"Bot initialized for {self.symbol}")
        except Exception as e:
            logging.error(f"Initialization failed: {e}")
            raise

    def start_trading(self):
        if not self.initialized:
            raise Exception("Bot not initialized")
        try:
            self.initial_balance = self.get_balance()
            logging.info(f"Financial data reset for new trading session on {self.symbol}")
            self.place_grid_orders()
            self.trading = True
            self.running = True
            socketio.start_background_task(self.monitor_and_adjust)
            logging.info(f"Trading started for {self.symbol}")
        except Exception as e:
            logging.error(f"Failed to start trading for {self.symbol}: {e}")
            raise

    def stop(self):
        if self.trading:
            try:
                self.exchange.cancel_all_orders(self.symbol)
                logging.info(f"All orders cancelled for {self.symbol}")
            except Exception as e:
                logging.error(f"Error cancelling orders for {self.symbol}: {e}")
        self.running = False
        self.trading = False
        self.initialized = False
        logging.info(f"Bot stopped for {self.symbol}")

    def get_balance(self):
        try:
            balance = self.exchange.fetch_balance()
            return {
                'base': float(f"{balance['total'].get(self.base, 0):.8f}"),
                'quote': float(f"{balance['total'].get(self.quote, 0):.8f}")
            }
        except Exception as e:
            logging.error(f"Error fetching balance for {self.symbol}: {e}")
            return {'base': 0.0, 'quote': 0.0}

    def place_grid_orders(self):
        current_price = self.exchange.fetch_ticker(self.symbol)['last']
        grid_interval = (self.upper_price - self.lower_price) / (self.grid_size - 1)
        amount_per_grid = (self.investment / 2) / self.grid_size / current_price

        open_orders = self.exchange.fetch_open_orders(self.symbol)
        open_buys = [order for order in open_orders if order['side'] == 'buy']
        has_sold = any(trade['side'] == 'sell' for trade in self.exchange.fetch_my_trades(self.symbol, limit=1000))

        for i in range(self.grid_size):
            price = self.lower_price + (i * grid_interval)
            if price < current_price:
                if len(open_buys) < self.max_open_buys and (has_sold or len(self.exchange.fetch_my_trades(self.symbol, limit=1000)) == 0):
                    try:
                        self.exchange.create_limit_buy_order(self.symbol, amount_per_grid, price)
                        logging.info(f"Placed buy order at {price} for {self.symbol}")
                    except Exception as e:
                        logging.error(f"Failed to place buy order for {self.symbol}: {e}")
            elif price > current_price:
                try:
                    self.exchange.create_limit_sell_order(self.symbol, amount_per_grid, price)
                    logging.info(f"Placed sell order at {price} for {self.symbol}")
                except Exception as e:
                    logging.error(f"Failed to place sell order for {self.symbol}: {e}")

    def monitor_and_adjust(self):
        while self.running:
            try:
                bots_stats = {
                    symbol: bot.get_statistics()
                    for symbol, bot in bots.items()
                    if bot.initialized
                }
                socketio.emit('update_stats', bots_stats)
                logging.info(f"Emitted stats update for all bots: {list(bots_stats.keys())}")
                time.sleep(60)
            except Exception as e:
                logging.error(f"Error in monitor_and_adjust for {self.symbol}: {e}")
                time.sleep(60)

    def get_statistics(self):
        try:
            with self.lock:
                balance = self.get_balance()
                open_orders = self.exchange.fetch_open_orders(self.symbol)
                trades = self.exchange.fetch_my_trades(self.symbol, limit=1000)
                current_price = float(f"{self.exchange.fetch_ticker(self.symbol)['last']:.8f}")

                initial_quote = self.initial_balance['quote'] if self.initial_balance else 0
                current_quote = balance['quote']
                floating_profit = float(f"{(balance['base'] * current_price - (self.investment / 2)):.8f}")
                grid_profit = float(f"{(sum(trade['cost'] for trade in trades if trade['side'] == 'sell') - sum(trade['cost'] for trade in trades if trade['side'] == 'buy')):.8f}")
                total_profit = float(f"{(current_quote - initial_quote + floating_profit):.8f}")

                total_profit_percent = float(f"{(total_profit / self.investment * 100 if self.investment != 0 else 0):.8f}")
                grid_profit_percent = float(f"{(grid_profit / self.investment * 100 if self.investment != 0 else 0):.8f}")
                floating_profit_percent = float(f"{(floating_profit / self.investment * 100 if self.investment != 0 else 0):.8f}")

                recent_trades = [
                    {
                        'timestamp': datetime.fromtimestamp(t['timestamp'] / 1000, tz=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S'),
                        'side': t['side'],
                        'price': f"{t['price']:.8f}",
                        'amount': f"{t['amount']:.8f}"
                    }
                    for t in trades[-10:]
                ]

                return {
                    'total_profit': f"{total_profit:.8f}",
                    'grid_profit': f"{grid_profit:.8f}",
                    'floating_profit': f"{floating_profit:.8f}",
                    'total_profit_percent': f"{total_profit_percent:.8f}",
                    'grid_profit_percent': f"{grid_profit_percent:.8f}",
                    'floating_profit_percent': f"{floating_profit_percent:.8f}",
                    'open_orders': [
                        {
                            'id': o['id'],
                            'side': o['side'],
                            'price': f"{o['price']:.8f}",
                            'amount': f"{o['amount']:.8f}"
                        }
                        for o in open_orders
                    ],
                    'recent_trades': recent_trades,
                    'current_price': f"{current_price:.8f}",
                    'quote': self.quote
                }
        except Exception as e:
            logging.error(f"Error fetching statistics for {self.symbol}: {e}")
            return {}

@app.route('/')
def index():
    logging.info("Rendering index page")
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_bot():
    global bots
    base = request.form['base'].upper()
    quote = request.form['quote'].upper()
    symbol = base + quote
    if symbol in bots and bots[symbol].initialized:
        return jsonify({'status': 'error', 'message': f'Bot already initialized for {symbol}'}), 400
    try:
        grid_size = int(request.form['grid_size'])
        percentage_range = float(request.form['percentage_range'])
        investment = float(request.form['investment'])
        max_open_buys = int(request.form['max_open_buys'])
        testnet = request.form.get('testnet') == 'on'

        exchange = get_exchange(testnet)
        current_price = exchange.fetch_ticker(symbol)['last']
        upper_price = current_price * (1 + percentage_range / 100)
        lower_price = current_price * (1 - percentage_range / 100)

        bots[symbol] = GridTradingBot(exchange, base, quote, grid_size, upper_price, lower_price, investment, max_open_buys)
        bots[symbol].initialize()
        return jsonify({'status': 'initialized', 'symbol': symbol})
    except Exception as e:
        logging.error(f"Start bot failed for {symbol}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/start_trading', methods=['POST'])
def start_trading():
    global bots
    symbol = request.form['symbol'].upper()
    if symbol not in bots or not bots[symbol].initialized:
        return jsonify({'status': 'error', 'message': f'Bot not found or not initialized for {symbol}'}), 400
    try:
        bots[symbol].start_trading()
        return jsonify({'status': 'trading started', 'symbol': symbol})
    except Exception as e:
        logging.error(f"Start trading failed for {symbol}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stop', methods=['POST'])
def stop_bot():
    global bots
    symbol = request.form['symbol'].upper()
    if symbol not in bots or not bots[symbol].initialized:
        return jsonify({'status': 'error', 'message': f'Bot not found or not initialized for {symbol}'}), 400
    try:
        bots[symbol].stop()
        del bots[symbol]
        return jsonify({'status': 'stopped', 'symbol': symbol})
    except Exception as e:
        logging.error(f"Stop bot failed for {symbol}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stop_trading', methods=['POST'])
def stop_trading():
    global bots
    symbol = request.form['symbol'].upper()
    if symbol not in bots or not bots[symbol].trading:
        return jsonify({'status': 'error', 'message': f'No trading in progress for {symbol}'}), 400
    try:
        bots[symbol].exchange.cancel_all_orders(bots[symbol].symbol)
        bots[symbol].trading = False
        logging.info(f"Trading stopped, all orders cancelled for {symbol}")
        return jsonify({'status': 'trading stopped', 'symbol': symbol})
    except Exception as e:
        logging.error(f"Stop trading failed for {symbol}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stats')
def stats():
    global bots
    symbol = request.args.get('symbol', '').upper()
    if symbol in bots and bots[symbol].initialized:
        stats = bots[symbol].get_statistics()
        logging.info(f"Rendering stats page for {symbol}")
        return render_template('stats.html', stats=stats, symbol=symbol)
    else:
        exchange = get_exchange()
        try:
            current_price = float(f"{exchange.fetch_ticker(symbol)['last']:.8f}")
            stats = {'current_price': f"{current_price:.8f}", 'quote': symbol[-4:]}
        except:
            stats = {'current_price': 'N/A', 'quote': symbol[-4:] if len(symbol) >= 4 else 'Unknown'}
        return render_template('stats.html', stats=stats, symbol=symbol)

@app.route('/status')
def status():
    global bots
    symbol = request.args.get('symbol', '').upper()
    if symbol and symbol in bots:
        return jsonify({
            'initialized': bots[symbol].initialized,
            'trading': bots[symbol].trading
        })
    else:
        return jsonify({
            symbol: {
                'initialized': bot.initialized,
                'trading': bot.trading
            } for symbol, bot in bots.items()
        })

@app.route('/get_active_pairs')
def get_active_pairs():
    active_pairs = [symbol for symbol, bot in bots.items() if bot.initialized]
    return jsonify(active_pairs)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)