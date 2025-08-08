import os
import io
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
import openpyxl
from openpyxl.styles import Font, Alignment
from flask import send_file
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

# --- App & Database Configuration ---
app = Flask(__name__)
# Use a consistent secret key for sessions to persist across server restarts
app.secret_key = 'your-secret-key-here-change-in-production-2024'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'calculator.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
getcontext().prec = 28

# --- Database Models ---
class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200))
    icl_start_date = db.Column(db.Date, nullable=False)
    icl_end_date = db.Column(db.Date, nullable=True)
    interest_rate = db.Column(db.Float, nullable=False, default=12.0)
    tds_rate = db.Column(db.Float, nullable=False, default=10.0)
    interest_type = db.Column(db.String(20), nullable=False, default='compound') # 'simple' or 'compound'
    frequency = db.Column(db.String(20), nullable=False, default='quarterly') # 'monthly', 'quarterly', 'yearly'
    penalty_rate = db.Column(db.Float, nullable=False, default=2.0) # Additional penalty rate for overdue
    status = db.Column(db.String(20), nullable=False, default='active') # 'active', 'overdue', 'closed', 'npa'
    closure_date = db.Column(db.Date, nullable=True)
    overdue_days = db.Column(db.Integer, nullable=False, default=0)
    grace_period = db.Column(db.Integer, nullable=False, default=30) # Grace period in days
    transactions = db.relationship('Transaction', backref='customer', cascade="all, delete-orphan")
    repayment_method = db.Column(db.String(20), nullable=False, default='exclusive') # 'inclusive' or 'exclusive'

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    paid = db.Column(db.Float, nullable=False, default=0.0)
    received = db.Column(db.Float, nullable=False, default=0.0)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')  # admin, manager, user
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# --- Template Filters ---
@app.template_filter('format_date')
def format_date_filter(date_obj):
    if not isinstance(date_obj, (datetime, timedelta)):
         date_obj = datetime.strptime(str(date_obj), '%Y-%m-%d').date()
    return date_obj.strftime('%d/%m/%Y')

@app.template_filter('format_currency')
def format_currency_filter(num):
    if num is None: return '₹ 0.00'
    try:
        return f"₹ {Decimal(num):,.2f}"
    except (ValueError, TypeError):
        return '₹ 0.00'

# --- Template Functions ---
@app.template_global()
def calculate_data_global(customer):
    """Make calculate_data available in templates"""
    return calculate_data(customer)

# --- Authentication Functions ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash('Please log in to access this page.', 'error')
                return redirect(url_for('login'))

            user = User.query.get(session['user_id'])
            if not user or user.role != role:
                flash(f'Access denied. {role.title()} role required.', 'error')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def admin_required(f):
    return role_required('admin')(f)

def get_current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None

# --- Helper Functions ---
def calculate_dashboard_stats(customers):
    """Calculate dashboard statistics"""
    active_loans = 0
    total_outstanding = Decimal('0')
    this_month_interest = Decimal('0')
    
    current_date = datetime.now().date()
    current_month_start = datetime(current_date.year, current_date.month, 1).date()
    
    for customer in customers:
        # Count active loans (not closed)
        if customer.status != 'closed':
            active_loans += 1
        
        # Calculate outstanding balance
        if customer.transactions:
            calculated_data = calculate_data(customer)
            if calculated_data:
                # Get the final outstanding balance
                final_outstanding = calculated_data[-1]['outstanding']
                total_outstanding += final_outstanding
                
                # Calculate this month's interest
                for entry in calculated_data:
                    if (entry['date'] >= current_month_start and 
                        entry['date'] <= current_date and 
                        entry.get('net')):
                        this_month_interest += entry['net']
    
    return {
        'active_loans': active_loans,
        'total_outstanding': total_outstanding,
        'this_month_interest': this_month_interest
    }

def get_recent_transactions_with_customer_names():
    """Get recent transactions with customer names"""
    recent_txs = db.session.query(Transaction, Customer.name).join(
        Customer, Transaction.customer_id == Customer.id
    ).order_by(Transaction.date.desc()).limit(5).all()
    
    transactions = []
    for tx, customer_name in recent_txs:
        transactions.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(str(tx.paid)),
            'received': Decimal(str(tx.received)),
            'customer_name': customer_name
        })
    
    return transactions

def get_quarter_info(date_obj, start_date_obj):
    months_diff = (date_obj.year - start_date_obj.year) * 12 + (date_obj.month - start_date_obj.month)
    quarter_index = months_diff // 3
    q_start_month = start_date_obj.month + quarter_index * 3
    q_start_year = start_date_obj.year + (q_start_month - 1) // 12
    q_start_month = (q_start_month - 1) % 12 + 1
    q_start_date = datetime(q_start_year, q_start_month, start_date_obj.day).date()
    q_end_month = q_start_date.month + 3
    q_end_year = q_start_date.year
    if q_end_month > 12:
        q_end_month -= 12
        q_end_year += 1
    q_end_date = datetime(q_end_year, q_end_month, 1).date() - timedelta(days=1)
    return {"name": f"Q{(quarter_index % 4) + 1} {q_start_date.year}", "startDate": q_start_date, "endDate": q_end_date}

def get_month_info(date_obj, start_date_obj):
    """Helper to get month start and end dates, respecting the start_date_obj's day."""
    # Calculate the month and year for the current transaction date
    current_month_year_start = datetime(date_obj.year, date_obj.month, start_date_obj.day).date()

    # Determine the start date of the current month's period
    month_start_date = current_month_year_start

    # Determine the end date of the current month's period
    # If it's the last month of the year, the end date is December 31st of the current year
    # Otherwise, it's the day before the first day of the next month
    if date_obj.month == 12:
        month_end_date = datetime(date_obj.year, 12, 31).date()
    else:
        next_month_start = datetime(date_obj.year, date_obj.month + 1, start_date_obj.day).date()
        month_end_date = next_month_start - timedelta(days=1)

    return {"name": f"{date_obj.strftime('%B')} {date_obj.year}", "startDate": month_start_date, "endDate": month_end_date}

def get_financial_year_info(date_obj, start_date_obj):
    """Helper to get financial year start and end dates, assuming FY starts April 1st."""
    # Determine the financial year based on the date
    if date_obj.month >= 4:
        fy_year = date_obj.year
        fy_start_date = datetime(fy_year, 4, 1).date()
        fy_end_date = datetime(fy_year + 1, 3, 31).date()
    else:
        fy_year = date_obj.year - 1
        fy_start_date = datetime(fy_year, 4, 1).date()
        fy_end_date = datetime(fy_year + 1, 3, 31).date()

    return {"name": f"FY{fy_year}-{fy_end_date.year}", "startDate": fy_start_date, "endDate": fy_end_date}


def is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)

def calculate_overdue_days(customer, current_date=None):
    """Calculate overdue days based on ICL end date and grace period"""
    if not customer.icl_end_date:
        return 0

    if not current_date:
        current_date = datetime.now().date()

    grace_end_date = customer.icl_end_date + timedelta(days=customer.grace_period)

    if current_date > grace_end_date:
        return (current_date - grace_end_date).days
    return 0

def calculate_penalty_interest(principal_amount, penalty_rate, overdue_days):
    """Calculate penalty interest for overdue amounts"""
    if overdue_days <= 0:
        return Decimal('0')

    # Industry standard: Penalty calculated on daily basis
    daily_penalty_rate = Decimal(penalty_rate) / Decimal('100') / Decimal('365')
    penalty_amount = principal_amount * daily_penalty_rate * Decimal(overdue_days)
    return penalty_amount

def update_loan_status(customer, current_date=None):
    """Update loan status based on overdue days and industry standards"""
    if not current_date:
        current_date = datetime.now().date()

    overdue_days = calculate_overdue_days(customer, current_date)
    customer.overdue_days = overdue_days

    if customer.status == 'closed':
        return customer.status

    # Check if loan period is still active (ICL end date not reached)
    if customer.icl_end_date and current_date <= customer.icl_end_date:
        customer.status = 'active'
        return customer.status

    # If no ICL end date is set, treat as ongoing loan
    if not customer.icl_end_date:
        customer.status = 'active'
        return customer.status

    # Only check overdue status after ICL period has ended
    if overdue_days == 0:
        customer.status = 'active'
    elif overdue_days <= 90:  # Industry standard: 1-90 days = overdue
        customer.status = 'overdue'
    else:  # > 90 days = Non-Performing Asset (NPA)
        customer.status = 'npa'

    return customer.status

# --- Calculation Engine ---
def calculate_data(customer):
    if not customer or not customer.transactions:
        return []

    # Update loan status before calculation
    update_loan_status(customer)

    settings = {
        'interestRate': Decimal(customer.interest_rate),
        'tdsRate': Decimal(customer.tds_rate),
        'penaltyRate': Decimal(customer.penalty_rate)
    }

    # Check interest type and use appropriate calculation
    if customer.interest_type == 'simple':
        if customer.frequency == 'monthly':
            return calculate_monthly_simple_interest_data(customer, settings)
        elif customer.frequency == 'quarterly':
            return calculate_simple_interest_data(customer, settings)
        elif customer.frequency == 'yearly':
            return calculate_yearly_simple_interest_data(customer, settings)
        else: # Default to quarterly if frequency is unrecognized
            return calculate_simple_interest_data(customer, settings)
    else:
        # Use appropriate compound interest calculation based on frequency
        if customer.frequency == 'monthly':
            return calculate_monthly_compound_interest_data(customer, settings)
        elif customer.frequency == 'quarterly':
            return calculate_compound_interest_data(customer, settings)
        elif customer.frequency == 'yearly':
            return calculate_yearly_compound_interest_data(customer, settings)
        else: # Default to quarterly if frequency is unrecognized
            return calculate_compound_interest_data(customer, settings)


def calculate_simple_interest_data(customer, settings):
    """Calculate data using simple interest logic with existing period splitting but interest on principal only"""
    sorted_txs = sorted(customer.transactions, key=lambda x: x.date)

    timeline = []
    running_outstanding = Decimal('0')
    current_quarter_net_interest = Decimal('0')
    principal_balance = Decimal('0')  # Track principal balance separately

    first_tx_date = sorted_txs[0].date
    last_quarter_info = get_quarter_info(first_tx_date, customer.icl_start_date)

    def add_missing_quarters(from_quarter_info, to_quarter_info, outstanding_balance, principal_bal):
        """Add missing quarters between two quarters for simple interest"""
        current_quarter = from_quarter_info

        while current_quarter['name'] != to_quarter_info['name']:
            # Calculate next quarter
            next_start_month = current_quarter['startDate'].month + 3
            next_start_year = current_quarter['startDate'].year
            if next_start_month > 12:
                next_start_month -= 12
                next_start_year += 1

            next_quarter_start = datetime(next_start_year, next_start_month, customer.icl_start_date.day).date()
            next_quarter_info = get_quarter_info(next_quarter_start, customer.icl_start_date)

            if next_quarter_info['name'] == to_quarter_info['name']:
                break

            # Check if missing quarter end date exceeds ICL end date
            quarter_end_date = next_quarter_info['endDate']
            if customer.icl_end_date and quarter_end_date > customer.icl_end_date:
                quarter_end_date = customer.icl_end_date

            # Calculate interest for the missing quarter on PRINCIPAL BALANCE ONLY
            days_in_quarter = (quarter_end_date - current_quarter['endDate']).days
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(current_quarter['endDate'].year):
                # Check if the quarter period includes Feb 29
                start_date = current_quarter['endDate']
                end_date = quarter_end_date
                feb_29 = datetime(current_quarter['endDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366

            quarter_interest = (principal_bal * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_quarter)) / Decimal(days_in_year)
            quarter_net = quarter_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Add missing quarter entry
            timeline.append({
                'id': f"missing-{next_quarter_info['name']}",
                'date': quarter_end_date,
                'description': f"{next_quarter_info['name']} Net Interest (No Transactions)",
                'paid': quarter_net,
                'received': Decimal('0'),
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + quarter_net,
                'principal_balance': principal_bal,
                'interest': quarter_interest,
                'tds': quarter_interest - quarter_net,
                'net': quarter_net,
                'days': days_in_quarter
            })

            outstanding_balance += quarter_net
            current_quarter = next_quarter_info

            # Stop if we've reached ICL end date
            if customer.icl_end_date and quarter_end_date >= customer.icl_end_date:
                break

        return outstanding_balance

    for i, tx in enumerate(sorted_txs):
        tx_date = tx.date
        tx_quarter_info = get_quarter_info(tx_date, customer.icl_start_date)

        # Check if this is the first transaction in a new quarter and if it's a repayment
        is_first_tx_in_quarter = tx_quarter_info['name'] != last_quarter_info['name']
        is_repayment = Decimal(tx.received) > Decimal('0')

        if is_first_tx_in_quarter:
            # Close previous quarter
            running_outstanding += current_quarter_net_interest
            timeline.append({
                'id': f"summary-{last_quarter_info['name']}",
                'date': last_quarter_info['endDate'],
                'description': f"{last_quarter_info['name']} Net Interest",
                'paid': current_quarter_net_interest, 
                'received': Decimal('0'), 
                'isSummary': True,
                'outstanding': running_outstanding,
                'principal_balance': principal_balance
            })

            # Add missing quarters if any
            running_outstanding = add_missing_quarters(last_quarter_info, tx_quarter_info, running_outstanding, principal_balance)

            # Add opening balance for current quarter (respecting ICL end date)
            opening_end_date = tx_date
            if customer.icl_end_date and opening_end_date > customer.icl_end_date:
                opening_end_date = customer.icl_end_date

            days_opening = (opening_end_date - tx_quarter_info['startDate']).days
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_quarter_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_quarter_info['startDate']
                end_date = opening_end_date
                feb_29 = datetime(tx_quarter_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366

            # Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
            interest_opening = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_opening)) / Decimal(days_in_year_opening)
            net_opening = interest_opening * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            timeline.append({
                'id': f"opening-{tx_quarter_info['name']}",
                'date': tx_quarter_info['startDate'],
                'description': 'Opening Balance',
                'paid': Decimal('0'), 
                'received': Decimal('0'), 
                'isOpening': True,
                'outstanding': running_outstanding,
                'principal_balance': principal_balance,
                'interest': interest_opening,
                'tds': interest_opening - net_opening,
                'net': net_opening
            })

            current_quarter_net_interest = net_opening
            last_quarter_info = tx_quarter_info

        # Update principal balance based on transaction
        principal_balance = principal_balance + Decimal(tx.paid) - Decimal(tx.received)
        outstanding_for_this_row = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

        next_tx = sorted_txs[i + 1] if i + 1 < len(sorted_txs) else None
        end_date = tx_quarter_info['endDate']
        is_last_tx_in_quarter = True

        # Respect ICL end date constraint - don't calculate interest beyond ICL end date
        if customer.icl_end_date and end_date > customer.icl_end_date:
            end_date = customer.icl_end_date
            is_last_tx_in_quarter = True

        if next_tx:
            next_tx_date = next_tx.date
            next_tx_quarter_info = get_quarter_info(next_tx_date, customer.icl_start_date)
            if next_tx_quarter_info['name'] == tx_quarter_info['name']:
                # Also check ICL end date for next transaction
                if not customer.icl_end_date or next_tx_date <= customer.icl_end_date:
                    end_date = next_tx_date
                    is_last_tx_in_quarter = False

        # Special condition for exclusive method: if first transaction is repayment on quarter end date, use inclusive logic
        use_inclusive_logic = False
        if customer.repayment_method == 'inclusive':
            use_inclusive_logic = True
        elif (customer.repayment_method == 'exclusive' and is_first_tx_in_quarter and 
              is_repayment and tx_date == tx_quarter_info['endDate']):
            use_inclusive_logic = True

        # For inclusive repayment method or special exclusive condition, adjust the calculation period
        if is_first_tx_in_quarter and is_repayment and use_inclusive_logic:
            # For inclusive method, calculate interest from quarter start to repayment date (including repayment date)
            opening_period_end = tx_date
            opening_days = (opening_period_end - tx_quarter_info['startDate']).days + 1  # +1 to include repayment date

            # Calculate the reduced principal balance after repayment
            # For simple interest, principal balance after this transaction
            reduced_principal_balance = principal_balance

            # Get the original principal balance before this transaction
            original_principal_balance = principal_balance - Decimal(tx.paid) + Decimal(tx.received)

            # Special condition: If repayment amount is larger than outstanding amount, 
            # use original principal balance for interest calculation
            balance_for_interest_calculation = reduced_principal_balance
            if Decimal(tx.received) > running_outstanding:
                balance_for_interest_calculation = original_principal_balance

            # Calculate interest for the opening period on the appropriate balance (quarter start to repayment date inclusive)
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_quarter_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_quarter_info['startDate']
                end_date = opening_period_end
                feb_29 = datetime(tx_quarter_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366

            opening_interest = (balance_for_interest_calculation * (settings['interestRate'] / Decimal('100')) * Decimal(opening_days)) / Decimal(days_in_year_opening)
            opening_net = opening_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Update the opening balance entry with correct interest calculation
            for entry in timeline:
                if entry.get('id') == f"opening-{tx_quarter_info['name']}":
                    entry['days'] = opening_days
                    entry['interest'] = opening_interest
                    entry['net'] = opening_net
                    entry['tds'] = opening_interest - opening_net
                    break

            # Update current quarter net interest
            current_quarter_net_interest = opening_net

            # Check if loan is fully repaid (outstanding becomes zero or negative)
            if outstanding_for_this_row <= Decimal('0'):
                # Loan is fully repaid, no further interest calculation
                days = 0
            else:
                # Now calculate remaining period after repayment (exclusive)
                calculation_start_date = tx_date + timedelta(days=1)
                days = (end_date - calculation_start_date).days
                if is_last_tx_in_quarter: days += 1

                # Ensure days is not negative
                if days < 0:
                    days = 0
        else:
            # Standard calculation for non-inclusive or non-first-repayment transactions
            days = (end_date - tx_date).days
            if is_last_tx_in_quarter: days += 1

        # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
        days_in_year = 365
        if is_leap(tx_date.year):
            # Check if the calculation period includes Feb 29
            start_date = tx_date
            end_date = tx_date + timedelta(days=days)
            feb_29 = datetime(tx_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366

        # SIMPLE INTEREST: Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
        interest = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days)) / Decimal(days_in_year)

        # Calculate penalty interest for overdue scenarios (on principal balance)
        penalty_interest = Decimal('0')
        if customer.icl_end_date and tx_date > customer.icl_end_date:
            overdue_days_for_tx = calculate_overdue_days(customer, tx_date)
            if overdue_days_for_tx > 0:
                penalty_interest = calculate_penalty_interest(principal_balance, customer.penalty_rate, min(overdue_days_for_tx, days))

        total_interest = interest + penalty_interest
        net = total_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
        current_quarter_net_interest += net

        timeline.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(tx.paid),
            'received': Decimal(tx.received),
            'days': days, 
            'interest': interest,
            'penalty_interest': penalty_interest,
            'total_interest': total_interest,
            'net': net, 
            'tds': total_interest - net, 
            'outstanding': outstanding_for_this_row,
            'principal_balance': principal_balance,
            'is_overdue': customer.icl_end_date and tx_date > customer.icl_end_date
        })
        running_outstanding = outstanding_for_this_row

    final_outstanding = running_outstanding + current_quarter_net_interest
    last_tx_date = sorted_txs[-1].date
    last_tx_quarter_info = get_quarter_info(last_tx_date, customer.icl_start_date)

    timeline.append({
        'id': f"summary-{last_tx_quarter_info['name']}",
        'date': last_tx_quarter_info['endDate'],
        'description': f"{last_tx_quarter_info['name']} Net Interest",
        'paid': current_quarter_net_interest, 
        'received': Decimal('0'), 
        'isSummary': True,
        'outstanding': final_outstanding,
        'principal_balance': principal_balance
    })

    return timeline

def calculate_monthly_simple_interest_data(customer, settings):
    """Calculate data using simple interest logic with monthly frequency and existing period splitting but interest on principal only"""
    sorted_txs = sorted(customer.transactions, key=lambda x: x.date)

    timeline = []
    running_outstanding = Decimal('0')
    current_month_net_interest = Decimal('0')
    principal_balance = Decimal('0')  # Track principal balance separately

    first_tx_date = sorted_txs[0].date
    last_month_info = get_month_info(first_tx_date, customer.icl_start_date)

    def add_missing_months(from_month_info, to_month_info, outstanding_balance, principal_bal):
        """Add missing months between two months for simple interest"""
        current_month = from_month_info

        while current_month['name'] != to_month_info['name']:
            # Calculate next month
            next_start_month_num = current_month['startDate'].month + 1
            next_start_year = current_month['startDate'].year
            if next_start_month_num > 12:
                next_start_month_num = 1
                next_start_year += 1

            # Use the day from the customer's start date for consistency
            next_month_start = datetime(next_start_year, next_start_month_num, customer.icl_start_date.day).date()
            next_month_info = get_month_info(next_month_start, customer.icl_start_date)

            if next_month_info['name'] == to_month_info['name']:
                break

            # Check if missing month end date exceeds ICL end date
            month_end_date = next_month_info['endDate']
            if customer.icl_end_date and month_end_date > customer.icl_end_date:
                month_end_date = customer.icl_end_date

            # Calculate interest for the missing month on PRINCIPAL BALANCE ONLY
            days_in_month = (month_end_date - current_month['endDate']).days
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(current_month['endDate'].year):
                # Check if the month period includes Feb 29
                start_date = current_month['endDate']
                end_date = month_end_date
                feb_29 = datetime(current_month['endDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366

            month_interest = (principal_bal * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_month)) / Decimal(days_in_year)
            month_net = month_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Add missing month entry
            timeline.append({
                'id': f"missing-{next_month_info['name']}",
                'date': month_end_date,
                'description': f"{next_month_info['name']} Net Interest (No Transactions)",
                'paid': month_net,
                'received': Decimal('0'),
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + month_net,
                'principal_balance': principal_bal,
                'interest': month_interest,
                'tds': month_interest - month_net,
                'net': month_net,
                'days': days_in_month
            })

            outstanding_balance += month_net
            current_month = next_month_info

            # Stop if we've reached ICL end date
            if customer.icl_end_date and month_end_date >= customer.icl_end_date:
                break

        return outstanding_balance

    for i, tx in enumerate(sorted_txs):
        tx_date = tx.date
        tx_month_info = get_month_info(tx_date, customer.icl_start_date)

        # Check if this is the first transaction in a new month and if it's a repayment
        is_first_tx_in_month = tx_month_info['name'] != last_month_info['name']
        is_repayment = Decimal(tx.received) > Decimal('0')

        if is_first_tx_in_month:
            # Close previous month
            running_outstanding += current_month_net_interest
            timeline.append({
                'id': f"summary-{last_month_info['name']}",
                'date': last_month_info['endDate'],
                'description': f"{last_month_info['name']} Net Interest",
                'paid': current_month_net_interest, 
                'received': Decimal('0'), 
                'isSummary': True,
                'outstanding': running_outstanding,
                'principal_balance': principal_balance
            })

            # Add missing months if any
            running_outstanding = add_missing_months(last_month_info, tx_month_info, running_outstanding, principal_balance)

            # Add opening balance for current month (respecting ICL end date)
            opening_end_date = tx_date
            if customer.icl_end_date and opening_end_date > customer.icl_end_date:
                opening_end_date = customer.icl_end_date

            days_opening = (opening_end_date - tx_month_info['startDate']).days
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_month_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_month_info['startDate']
                end_date = opening_end_date
                feb_29 = datetime(tx_month_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366

            # Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
            interest_opening = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_opening)) / Decimal(days_in_year_opening)
            net_opening = interest_opening * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            timeline.append({
                'id': f"opening-{tx_month_info['name']}",
                'date': tx_month_info['startDate'],
                'description': 'Opening Balance',
                'paid': Decimal('0'), 
                'received': Decimal('0'), 
                'isOpening': True,
                'outstanding': running_outstanding,
                'principal_balance': principal_balance,
                'interest': interest_opening,
                'tds': interest_opening - net_opening,
                'net': net_opening
            })

            current_month_net_interest = net_opening
            last_month_info = tx_month_info

        # Update principal balance based on transaction
        principal_balance = principal_balance + Decimal(tx.paid) - Decimal(tx.received)
        outstanding_for_this_row = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

        next_tx = sorted_txs[i + 1] if i + 1 < len(sorted_txs) else None
        end_date = tx_month_info['endDate']
        is_last_tx_in_month = True

        # Respect ICL end date constraint - don't calculate interest beyond ICL end date
        if customer.icl_end_date and end_date > customer.icl_end_date:
            end_date = customer.icl_end_date
            is_last_tx_in_month = True

        if next_tx:
            next_tx_date = next_tx.date
            next_tx_month_info = get_month_info(next_tx_date, customer.icl_start_date)
            if next_tx_month_info['name'] == tx_month_info['name']:
                # Also check ICL end date for next transaction
                if not customer.icl_end_date or next_tx_date <= customer.icl_end_date:
                    end_date = next_tx_date
                    is_last_tx_in_month = False

        # Special condition for exclusive method: if first transaction is repayment on month end date, use inclusive logic
        use_inclusive_logic = False
        if customer.repayment_method == 'inclusive':
            use_inclusive_logic = True
        elif (customer.repayment_method == 'exclusive' and is_first_tx_in_month and 
              is_repayment and tx_date == tx_month_info['endDate']):
            use_inclusive_logic = True

        # For inclusive repayment method or special exclusive condition, adjust the calculation period
        if is_first_tx_in_month and is_repayment and use_inclusive_logic:
            # For inclusive method, calculate interest from month start to repayment date (including repayment date)
            opening_period_end = tx_date
            opening_days = (opening_period_end - tx_month_info['startDate']).days + 1  # +1 to include repayment date

            # Calculate the reduced principal balance after repayment
            # For simple interest, principal balance after this transaction
            reduced_principal_balance = principal_balance

            # Get the original principal balance before this transaction
            original_principal_balance = principal_balance - Decimal(tx.paid) + Decimal(tx.received)

            # Special condition: If repayment amount is larger than outstanding amount, 
            # use original principal balance for interest calculation
            balance_for_interest_calculation = reduced_principal_balance
            if Decimal(tx.received) > running_outstanding:
                balance_for_interest_calculation = original_principal_balance

            # Calculate interest for the opening period on the appropriate balance (month start to repayment date inclusive)
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_month_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_month_info['startDate']
                end_date = opening_period_end
                feb_29 = datetime(tx_month_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366

            opening_interest = (balance_for_interest_calculation * (settings['interestRate'] / Decimal('100')) * Decimal(opening_days)) / Decimal(days_in_year_opening)
            opening_net = opening_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Update the opening balance entry with correct interest calculation
            for entry in timeline:
                if entry.get('id') == f"opening-{tx_month_info['name']}":
                    entry['days'] = opening_days
                    entry['interest'] = opening_interest
                    entry['net'] = opening_net
                    entry['tds'] = opening_interest - net_opening
                    break

            # Update current month net interest
            current_month_net_interest = opening_net

            # Check if loan is fully repaid (outstanding becomes zero or negative)
            if outstanding_for_this_row <= Decimal('0'):
                # Loan is fully repaid, no further interest calculation
                days = 0
            else:
                # Now calculate remaining period after repayment (exclusive)
                calculation_start_date = tx_date + timedelta(days=1)
                days = (end_date - calculation_start_date).days
                if is_last_tx_in_month: days += 1

                # Ensure days is not negative
                if days < 0:
                    days = 0
        else:
            # Standard calculation for non-inclusive or non-first-repayment transactions
            days = (end_date - tx_date).days
            if is_last_tx_in_month: days += 1

        # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
        days_in_year = 365
        if is_leap(tx_date.year):
            # Check if the calculation period includes Feb 29
            start_date = tx_date
            end_date = tx_date + timedelta(days=days)
            feb_29 = datetime(tx_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366

        # SIMPLE INTEREST: Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
        interest = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days)) / Decimal(days_in_year)

        # Calculate penalty interest for overdue scenarios (on principal balance)
        penalty_interest = Decimal('0')
        if customer.icl_end_date and tx_date > customer.icl_end_date:
            overdue_days_for_tx = calculate_overdue_days(customer, tx_date)
            if overdue_days_for_tx > 0:
                penalty_interest = calculate_penalty_interest(principal_balance, customer.penalty_rate, min(overdue_days_for_tx, days))

        total_interest = interest + penalty_interest
        net = total_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
        current_month_net_interest += net

        timeline.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(tx.paid),
            'received': Decimal(tx.received),
            'days': days, 
            'interest': interest,
            'penalty_interest': penalty_interest,
            'total_interest': total_interest,
            'net': net, 
            'tds': total_interest - net, 
            'outstanding': outstanding_for_this_row,
            'principal_balance': principal_balance,
            'is_overdue': customer.icl_end_date and tx_date > customer.icl_end_date
        })
        running_outstanding = outstanding_for_this_row

    final_outstanding = running_outstanding + current_month_net_interest
    last_tx_date = sorted_txs[-1].date
    last_tx_month_info = get_month_info(last_tx_date, customer.icl_start_date)

    timeline.append({
        'id': f"summary-{last_tx_month_info['name']}",
        'date': last_tx_month_info['endDate'],
        'description': f"{last_tx_month_info['name']} Net Interest",
        'paid': current_month_net_interest, 
        'received': Decimal('0'), 
        'isSummary': True,
        'outstanding': final_outstanding,
        'principal_balance': principal_balance
    })

    return timeline

def calculate_yearly_simple_interest_data(customer, settings):
    """Calculate data using simple interest logic with yearly frequency and existing period splitting but interest on principal only"""
    sorted_txs = sorted(customer.transactions, key=lambda x: x.date)

    timeline = []
    running_outstanding = Decimal('0')
    current_year_net_interest = Decimal('0')
    principal_balance = Decimal('0')  # Track principal balance separately

    first_tx_date = sorted_txs[0].date
    last_year_info = get_financial_year_info(first_tx_date, customer.icl_start_date)

    def add_missing_years(from_year_info, to_year_info, outstanding_balance, principal_bal):
        """Add missing financial years between two years for simple interest"""
        current_year = from_year_info

        while current_year['name'] != to_year_info['name']:
            # Calculate next financial year
            next_year_start = datetime(current_year['startDate'].year + 1, 4, 1).date()
            next_year_info = get_financial_year_info(next_year_start, customer.icl_start_date)

            if next_year_info['name'] == to_year_info['name']:
                break

            # Check if missing year end date exceeds ICL end date
            year_end_date = next_year_info['endDate']
            if customer.icl_end_date and year_end_date > customer.icl_end_date:
                year_end_date = customer.icl_end_date

            # Calculate interest for the missing year on PRINCIPAL BALANCE ONLY
            days_in_year_period = (year_end_date - current_year['endDate']).days
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(current_year['endDate'].year):
                # Check if the year period includes Feb 29
                start_date = current_year['endDate']
                end_date = year_end_date
                feb_29 = datetime(current_year['endDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366

            year_interest = (principal_bal * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_year_period)) / Decimal(days_in_year)
            year_net = year_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Add missing year entry
            timeline.append({
                'id': f"missing-{next_year_info['name']}",
                'date': year_end_date,
                'description': f"{next_year_info['name']} Net Interest (No Transactions)",
                'paid': year_net,
                'received': Decimal('0'),
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + year_net,
                'principal_balance': principal_bal,
                'interest': year_interest,
                'tds': year_interest - year_net,
                'net': year_net,
                'days': days_in_year_period
            })

            outstanding_balance += year_net
            current_year = next_year_info

            # Stop if we've reached ICL end date
            if customer.icl_end_date and year_end_date >= customer.icl_end_date:
                break

        return outstanding_balance

    for i, tx in enumerate(sorted_txs):
        tx_date = tx.date
        tx_year_info = get_financial_year_info(tx_date, customer.icl_start_date)

        # Check if this is the first transaction in a new year and if it's a repayment
        is_first_tx_in_year = tx_year_info['name'] != last_year_info['name']
        is_repayment = Decimal(tx.received) > Decimal('0')

        if is_first_tx_in_year:
            # Close previous year
            running_outstanding += current_year_net_interest
            timeline.append({
                'id': f"summary-{last_year_info['name']}",
                'date': last_year_info['endDate'],
                'description': f"{last_year_info['name']} Net Interest",
                'paid': current_year_net_interest, 
                'received': Decimal('0'), 
                'isSummary': True,
                'outstanding': running_outstanding,
                'principal_balance': principal_balance
            })

            # Add missing years if any
            running_outstanding = add_missing_years(last_year_info, tx_year_info, running_outstanding, principal_balance)

            # Add opening balance for current year (respecting ICL end date)
            opening_end_date = tx_date
            if customer.icl_end_date and opening_end_date > customer.icl_end_date:
                opening_end_date = customer.icl_end_date

            days_opening = (opening_end_date - tx_year_info['startDate']).days
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_year_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_year_info['startDate']
                end_date = opening_end_date
                feb_29 = datetime(tx_year_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366

            # Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
            interest_opening = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_opening)) / Decimal(days_in_year_opening)
            net_opening = interest_opening * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            timeline.append({
                'id': f"opening-{tx_year_info['name']}",
                'date': tx_year_info['startDate'],
                'description': 'Opening Balance',
                'paid': Decimal('0'), 
                'received': Decimal('0'), 
                'isOpening': True,
                'outstanding': running_outstanding,
                'principal_balance': principal_balance,
                'interest': interest_opening,
                'tds': interest_opening - net_opening,
                'net': net_opening
            })

            current_year_net_interest = net_opening
            last_year_info = tx_year_info

        # Update principal balance based on transaction
        principal_balance = principal_balance + Decimal(tx.paid) - Decimal(tx.received)
        outstanding_for_this_row = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

        next_tx = sorted_txs[i + 1] if i + 1 < len(sorted_txs) else None
        end_date = tx_year_info['endDate']
        is_last_tx_in_year = True

        # Respect ICL end date constraint - don't calculate interest beyond ICL end date
        if customer.icl_end_date and end_date > customer.icl_end_date:
            end_date = customer.icl_end_date
            is_last_tx_in_year = True

        if next_tx:
            next_tx_date = next_tx.date
            next_tx_year_info = get_financial_year_info(next_tx_date, customer.icl_start_date)
            if next_tx_year_info['name'] == tx_year_info['name']:
                # Also check ICL end date for next transaction
                if not customer.icl_end_date or next_tx_date <= customer.icl_end_date:
                    end_date = next_tx_date
                    is_last_tx_in_year = False

        # Special condition for exclusive method: if first transaction is repayment on year end date, use inclusive logic
        use_inclusive_logic = False
        if customer.repayment_method == 'inclusive':
            use_inclusive_logic = True
        elif (customer.repayment_method == 'exclusive' and is_first_tx_in_year and 
              is_repayment and tx_date == tx_year_info['endDate']):
            use_inclusive_logic = True

        # For inclusive repayment method or special exclusive condition, adjust the calculation period
        if is_first_tx_in_year and is_repayment and use_inclusive_logic:
            # For inclusive method, calculate interest from year start to repayment date (including repayment date)
            opening_period_end = tx_date
            opening_days = (opening_period_end - tx_year_info['startDate']).days + 1  # +1 to include repayment date

            # Calculate the reduced principal balance after repayment
            # For simple interest, principal balance after this transaction
            reduced_principal_balance = principal_balance

            # Get the original principal balance before this transaction
            original_principal_balance = principal_balance - Decimal(tx.paid) + Decimal(tx.received)

            # Special condition: If repayment amount is larger than outstanding amount, 
            # use original principal balance for interest calculation
            balance_for_interest_calculation = reduced_principal_balance
            if Decimal(tx.received) > running_outstanding:
                balance_for_interest_calculation = original_principal_balance

            # Calculate interest for the opening period on the appropriate balance (year start to repayment date inclusive)
            # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_year_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_year_info['startDate']
                end_date = opening_period_end
                feb_29 = datetime(tx_year_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366

            opening_interest = (balance_for_interest_calculation * (settings['interestRate'] / Decimal('100')) * Decimal(opening_days)) / Decimal(days_in_year_opening)
            opening_net = opening_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Update the opening balance entry with correct interest calculation
            for entry in timeline:
                if entry.get('id') == f"opening-{tx_year_info['name']}":
                    entry['days'] = opening_days
                    entry['interest'] = opening_interest
                    entry['net'] = opening_net
                    entry['tds'] = opening_interest - opening_net
                    break

            # Update current year net interest
            current_year_net_interest = opening_net

            # Check if loan is fully repaid (outstanding becomes zero or negative)
            if outstanding_for_this_row <= Decimal('0'):
                # Loan is fully repaid, no further interest calculation
                days = 0
            else:
                # Now calculate remaining period after repayment (exclusive)
                calculation_start_date = tx_date + timedelta(days=1)
                days = (end_date - calculation_start_date).days
                if is_last_tx_in_year: days += 1

                # Ensure days is not negative
                if days < 0:
                    days = 0
        else:
            # Standard calculation for non-inclusive or non-first-repayment transactions
            days = (end_date - tx_date).days
            if is_last_tx_in_year: days += 1

        # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
        days_in_year = 365
        if is_leap(tx_date.year):
            # Check if the calculation period includes Feb 29
            start_date = tx_date
            end_date = tx_date + timedelta(days=days)
            feb_29 = datetime(tx_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366

        # SIMPLE INTEREST: Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
        interest = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days)) / Decimal(days_in_year)

        # Calculate penalty interest for overdue scenarios (on principal balance)
        penalty_interest = Decimal('0')
        if customer.icl_end_date and tx_date > customer.icl_end_date:
            overdue_days_for_tx = calculate_overdue_days(customer, tx_date)
            if overdue_days_for_tx > 0:
                penalty_interest = calculate_penalty_interest(principal_balance, customer.penalty_rate, min(overdue_days_for_tx, days))

        total_interest = interest + penalty_interest
        net = total_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
        current_year_net_interest += net

        timeline.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(tx.paid),
            'received': Decimal(tx.received),
            'days': days, 
            'interest': interest,
            'penalty_interest': penalty_interest,
            'total_interest': total_interest,
            'net': net, 
            'tds': total_interest - net, 
            'outstanding': outstanding_for_this_row,
            'principal_balance': principal_balance,
            'is_overdue': customer.icl_end_date and tx_date > customer.icl_end_date
        })
        running_outstanding = outstanding_for_this_row

    final_outstanding = running_outstanding + current_year_net_interest
    last_tx_date = sorted_txs[-1].date
    last_tx_year_info = get_financial_year_info(last_tx_date, customer.icl_start_date)

    timeline.append({
        'id': f"summary-{last_tx_year_info['name']}",
        'date': last_tx_year_info['endDate'],
        'description': f"{last_tx_year_info['name']} Net Interest",
        'paid': current_year_net_interest, 
        'received': Decimal('0'), 
        'isSummary': True,
        'outstanding': final_outstanding,
        'principal_balance': principal_balance
    })

    return timeline

def calculate_compound_interest_data(customer, settings):
    """Calculate data using existing compound interest logic"""
    sorted_txs = sorted(customer.transactions, key=lambda x: x.date)

    timeline = []
    running_outstanding = Decimal('0')
    current_quarter_net_interest = Decimal('0')

    first_tx_date = sorted_txs[0].date
    last_quarter_info = get_quarter_info(first_tx_date, customer.icl_start_date)

    def add_missing_quarters(from_quarter_info, to_quarter_info, outstanding_balance):
        """Add missing quarters between two quarters"""
        current_quarter = from_quarter_info

        while current_quarter['name'] != to_quarter_info['name']:
            # Calculate next quarter
            next_start_month = current_quarter['startDate'].month + 3
            next_start_year = current_quarter['startDate'].year
            if next_start_month > 12:
                next_start_month -= 12
                next_start_year += 1

            next_quarter_start = datetime(next_start_year, next_start_month, customer.icl_start_date.day).date()
            next_quarter_info = get_quarter_info(next_quarter_start, customer.icl_start_date)

            if next_quarter_info['name'] == to_quarter_info['name']:
                break

            # Check if missing quarter end date exceeds ICL end date
            quarter_end_date = next_quarter_info['endDate']
            if customer.icl_end_date and quarter_end_date > customer.icl_end_date:
                quarter_end_date = customer.icl_end_date

            # Calculate interest for the missing quarter (respecting ICL end date)
            days_in_quarter = (quarter_end_date - current_quarter['endDate']).days
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(current_quarter['endDate'].year):
                # Check if the quarter period includes Feb 29
                start_date = current_quarter['endDate']
                end_date = quarter_end_date
                feb_29 = datetime(current_quarter['endDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366
            quarter_interest = (outstanding_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_quarter)) / Decimal(days_in_year)
            quarter_net = quarter_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Add missing quarter entry
            timeline.append({
                'id': f"missing-{next_quarter_info['name']}",
                'date': quarter_end_date,
                'description': f"{next_quarter_info['name']} Net Interest (No Transactions)",
                'paid': quarter_net,
                'received': Decimal('0'),
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + quarter_net,
                'interest': quarter_interest,
                'tds': quarter_interest - quarter_net,
                'net': quarter_net,
                'days': days_in_quarter
            })

            outstanding_balance += quarter_net
            current_quarter = next_quarter_info

            # Stop if we've reached ICL end date
            if customer.icl_end_date and quarter_end_date >= customer.icl_end_date:
                break

        return outstanding_balance

    for i, tx in enumerate(sorted_txs):
        tx_date = tx.date
        tx_quarter_info = get_quarter_info(tx_date, customer.icl_start_date)

        # Check if this is the first transaction in a new quarter and if it's a repayment
        is_first_tx_in_quarter = tx_quarter_info['name'] != last_quarter_info['name']
        is_repayment = Decimal(tx.received) > Decimal('0')

        if is_first_tx_in_quarter:
            # Close previous quarter
            running_outstanding += current_quarter_net_interest
            timeline.append({'id': f"summary-{last_quarter_info['name']}",'date': last_quarter_info['endDate'],'description': f"{last_quarter_info['name']} Net Interest",'paid': current_quarter_net_interest, 'received': Decimal('0'), 'isSummary': True,'outstanding': running_outstanding})

            # Add missing quarters if any
            running_outstanding = add_missing_quarters(last_quarter_info, tx_quarter_info, running_outstanding)

            # Add opening balance for current quarter (respecting ICL end date)
            opening_end_date = tx_date
            if customer.icl_end_date and opening_end_date > customer.icl_end_date:
                opening_end_date = customer.icl_end_date

            days_opening = (opening_end_date - tx_quarter_info['startDate']).days
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_quarter_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_quarter_info['startDate']
                end_date = opening_end_date
                feb_29 = datetime(tx_quarter_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366
            interest_opening = (running_outstanding * (settings['interestRate'] / Decimal('100')) * Decimal(days_opening)) / Decimal(days_in_year_opening)
            net_opening = interest_opening * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            timeline.append({'id': f"opening-{tx_quarter_info['name']}",'date': tx_quarter_info['startDate'],'description': 'Opening Balance','paid': Decimal('0'), 'received': Decimal('0'), 'isOpening': True,'outstanding': running_outstanding,'days': days_opening,'interest': interest_opening,'tds': interest_opening - net_opening,'net': net_opening})

            current_quarter_net_interest = net_opening
            last_quarter_info = tx_quarter_info

        outstanding_for_this_row = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

        next_tx = sorted_txs[i + 1] if i + 1 < len(sorted_txs) else None
        end_date = tx_quarter_info['endDate']
        is_last_tx_in_quarter = True

        # Respect ICL end date constraint - don't calculate interest beyond ICL end date
        if customer.icl_end_date and end_date > customer.icl_end_date:
            end_date = customer.icl_end_date
            is_last_tx_in_quarter = True

        if next_tx:
            next_tx_date = next_tx.date
            next_tx_quarter_info = get_quarter_info(next_tx_date, customer.icl_start_date)
            if next_tx_quarter_info['name'] == tx_quarter_info['name']:
                # Also check ICL end date for next transaction
                if not customer.icl_end_date or next_tx_date <= customer.icl_end_date:
                    end_date = next_tx_date
                    is_last_tx_in_quarter = False

        # Special condition for exclusive method: if first transaction is repayment on quarter end date, use inclusive logic
        use_inclusive_logic = False
        if customer.repayment_method == 'inclusive':
            use_inclusive_logic = True
        elif (customer.repayment_method == 'exclusive' and is_first_tx_in_quarter and 
              is_repayment and tx_date == tx_quarter_info['endDate']):
            use_inclusive_logic = True

        # For inclusive repayment method or special exclusive condition, adjust the calculation period
        if is_first_tx_in_quarter and is_repayment and use_inclusive_logic:
            # For inclusive method, calculate interest from quarter start to repayment date (including repayment date)
            opening_period_end = tx_date
            opening_days = (opening_period_end - tx_quarter_info['startDate']).days + 1  # +1 to include repayment date

            # Calculate the reduced outstanding balance after repayment
            reduced_outstanding_balance = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

            # Special condition: If repayment amount is larger than outstanding amount, 
            # use original outstanding balance for interest calculation
            balance_for_interest_calculation = reduced_outstanding_balance
            if Decimal(tx.received) > running_outstanding:
                balance_for_interest_calculation = running_outstanding

            # Calculate interest for the opening period on the appropriate balance (quarter start to repayment date inclusive)
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_quarter_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_quarter_info['startDate']
                end_date = opening_period_end
                feb_29 = datetime(tx_quarter_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366
            opening_interest = (balance_for_interest_calculation * (settings['interestRate'] / Decimal('100')) * Decimal(opening_days)) / Decimal(days_in_year_opening)
            opening_net = opening_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Update the opening balance entry with correct interest calculation
            for entry in timeline:
                if entry.get('id') == f"opening-{tx_quarter_info['name']}":
                    entry['days'] = opening_days
                    entry['interest'] = opening_interest
                    entry['net'] = opening_net
                    entry['tds'] = opening_interest - opening_net
                    break

            # Update current quarter net interest
            current_quarter_net_interest = opening_net

            # Check if loan is fully repaid (outstanding becomes zero or negative)
            if outstanding_for_this_row <= Decimal('0'):
                # Loan is fully repaid, no further interest calculation
                days = 0
            else:
                # Now calculate remaining period after repayment (exclusive)
                calculation_start_date = tx_date + timedelta(days=1)
                days = (end_date - calculation_start_date).days
                if is_last_tx_in_quarter: days += 1

                # Ensure days is not negative
                if days < 0:
                    days = 0
        else:
            # Standard calculation for non-inclusive or non-first-repayment transactions
            days = (end_date - tx_date).days
            if is_last_tx_in_quarter: days += 1

        # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
        days_in_year = 365
        if is_leap(tx_date.year):
            # Check if the calculation period includes Feb 29
            start_date = tx_date
            end_date = tx_date + timedelta(days=days)
            feb_29 = datetime(tx_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366
        interest = (outstanding_for_this_row * (settings['interestRate'] / Decimal('100')) * Decimal(days)) / Decimal(days_in_year)

        # Calculate penalty interest for overdue scenarios
        penalty_interest = Decimal('0')
        if customer.icl_end_date and tx_date > customer.icl_end_date:
            overdue_days_for_tx = calculate_overdue_days(customer, tx_date)
            if overdue_days_for_tx > 0:
                penalty_interest = calculate_penalty_interest(outstanding_for_this_row, customer.penalty_rate, min(overdue_days_for_tx, days))

        total_interest = interest + penalty_interest
        net = total_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
        current_quarter_net_interest += net

        timeline.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(tx.paid),
            'received': Decimal(tx.received),
            'days': days, 
            'interest': interest,
            'penalty_interest': penalty_interest,
            'total_interest': total_interest,
            'net': net, 
            'tds': total_interest - net, 
            'outstanding': outstanding_for_this_row,
            'is_overdue': customer.icl_end_date and tx_date > customer.icl_end_date
        })
        running_outstanding = outstanding_for_this_row

    final_outstanding = running_outstanding + current_quarter_net_interest
    last_tx_date = sorted_txs[-1].date
    last_tx_quarter_info = get_quarter_info(last_tx_date, customer.icl_start_date)

    timeline.append({'id': f"summary-{last_tx_quarter_info['name']}",'date': last_tx_quarter_info['endDate'],'description': f"{last_tx_quarter_info['name']} Net Interest",'paid': current_quarter_net_interest, 'received': Decimal('0'), 'isSummary': True,'outstanding': final_outstanding})

    return timeline

def calculate_monthly_compound_interest_data(customer, settings):
    """Calculate data using monthly compound interest logic"""
    sorted_txs = sorted(customer.transactions, key=lambda x: x.date)

    timeline = []
    running_outstanding = Decimal('0')
    current_month_net_interest = Decimal('0')

    first_tx_date = sorted_txs[0].date
    last_month_info = get_month_info(first_tx_date, customer.icl_start_date)

    def add_missing_months(from_month_info, to_month_info, outstanding_balance):
        """Add missing months between two months"""
        current_month = from_month_info

        while current_month['name'] != to_month_info['name']:
            # Calculate next month
            next_start_month_num = current_month['startDate'].month + 1
            next_start_year = current_month['startDate'].year
            if next_start_month_num > 12:
                next_start_month_num = 1
                next_start_year += 1

            # Use the day from the customer's start date for consistency
            next_month_start = datetime(next_start_year, next_start_month_num, customer.icl_start_date.day).date()
            next_month_info = get_month_info(next_month_start, customer.icl_start_date)

            if next_month_info['name'] == to_month_info['name']:
                break

            # Check if missing month end date exceeds ICL end date
            month_end_date = next_month_info['endDate']
            if customer.icl_end_date and month_end_date > customer.icl_end_date:
                month_end_date = customer.icl_end_date

            # Calculate interest for the missing month
            days_in_month = (month_end_date - current_month['endDate']).days
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(current_month['endDate'].year):
                # Check if the quarter period includes Feb 29
                start_date = current_month['endDate']
                end_date = month_end_date
                feb_29 = datetime(current_month['endDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366
            month_interest = (outstanding_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_month)) / Decimal(days_in_year)
            month_net = month_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Add missing month entry
            timeline.append({
                'id': f"missing-{next_month_info['name']}",
                'date': month_end_date,
                'description': f"{next_month_info['name']} Net Interest (No Transactions)",
                'paid': month_net,
                'received': Decimal('0'),
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + month_net,
                'interest': month_interest,
                'tds': month_interest - month_net,
                'net': month_net,
                'days': days_in_month
            })

            outstanding_balance += month_net
            current_month = next_month_info

            # Stop if we've reached ICL end date
            if customer.icl_end_date and month_end_date >= customer.icl_end_date:
                break

        return outstanding_balance

    for i, tx in enumerate(sorted_txs):
        tx_date = tx.date
        tx_month_info = get_month_info(tx_date, customer.icl_start_date)

        # Check if this is the first transaction in a new month and if it's a repayment
        is_first_tx_in_month = tx_month_info['name'] != last_month_info['name']
        is_repayment = Decimal(tx.received) > Decimal('0')

        if is_first_tx_in_month:
            # Close previous month
            running_outstanding += current_month_net_interest
            timeline.append({'id': f"summary-{last_month_info['name']}",'date': last_month_info['endDate'],'description': f"{last_month_info['name']} Net Interest",'paid': current_month_net_interest, 'received': Decimal('0'), 'isSummary': True,'outstanding': running_outstanding})

            # Add missing months if any
            running_outstanding = add_missing_months(last_month_info, tx_month_info, running_outstanding)

            # Add opening balance for current month
            opening_end_date = tx_date
            if customer.icl_end_date and opening_end_date > customer.icl_end_date:
                opening_end_date = customer.icl_end_date

            days_opening = (opening_end_date - tx_month_info['startDate']).days
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_month_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_month_info['startDate']
                end_date = opening_end_date
                feb_29 = datetime(tx_month_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366
            interest_opening = (running_outstanding * (settings['interestRate'] / Decimal('100')) * Decimal(days_opening)) / Decimal(days_in_year_opening)
            net_opening = interest_opening * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            timeline.append({'id': f"opening-{tx_month_info['name']}",'date': tx_month_info['startDate'],'description': 'Opening Balance','paid': Decimal('0'), 'received': Decimal('0'), 'isOpening': True,'outstanding': running_outstanding,'days': days_opening,'interest': interest_opening,'tds': interest_opening - net_opening,'net': net_opening})

            current_month_net_interest = net_opening
            last_month_info = tx_month_info

        outstanding_for_this_row = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

        next_tx = sorted_txs[i + 1] if i + 1 < len(sorted_txs) else None
        end_date = tx_month_info['endDate']
        is_last_tx_in_month = True

        # Respect ICL end date constraint
        if customer.icl_end_date and end_date > customer.icl_end_date:
            end_date = customer.icl_end_date
            is_last_tx_in_month = True

        if next_tx:
            next_tx_date = next_tx.date
            next_tx_month_info = get_month_info(next_tx_date, customer.icl_start_date)
            if next_tx_month_info['name'] == tx_month_info['name']:
                if not customer.icl_end_date or next_tx_date <= customer.icl_end_date:
                    end_date = next_tx_date
                    is_last_tx_in_month = False

        # Special condition for exclusive method: if first transaction is repayment on month end date, use inclusive logic
        use_inclusive_logic = False
        if customer.repayment_method == 'inclusive':
            use_inclusive_logic = True
        elif (customer.repayment_method == 'exclusive' and is_first_tx_in_month and 
              is_repayment and tx_date == tx_month_info['endDate']):
            use_inclusive_logic = True

        # For inclusive repayment method or special exclusive condition, adjust the calculation period
        if is_first_tx_in_month and is_repayment and use_inclusive_logic:
            # For inclusive method, calculate interest from month start to repayment date (including repayment date)
            opening_period_end = tx_date
            opening_days = (opening_period_end - tx_month_info['startDate']).days + 1  # +1 to include repayment date

            # Calculate the reduced outstanding balance after repayment
            reduced_outstanding_balance = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

            # Special condition: If repayment amount is larger than outstanding amount, 
            # use original outstanding balance for interest calculation
            balance_for_interest_calculation = reduced_outstanding_balance
            if Decimal(tx.received) > running_outstanding:
                balance_for_interest_calculation = running_outstanding

            # Calculate interest for the opening period on the appropriate balance (month start to repayment date inclusive)
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_month_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_month_info['startDate']
                end_date = opening_period_end
                feb_29 = datetime(tx_month_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366
            opening_interest = (balance_for_interest_calculation * (settings['interestRate'] / Decimal('100')) * Decimal(opening_days)) / Decimal(days_in_year_opening)
            opening_net = opening_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Update the opening balance entry with correct interest calculation
            for entry in timeline:
                if entry.get('id') == f"opening-{tx_month_info['name']}":
                    entry['days'] = opening_days
                    entry['interest'] = opening_interest
                    entry['net'] = opening_net
                    entry['tds'] = opening_interest - net_opening
                    break

            # Update current month net interest
            current_month_net_interest = opening_net

            # Check if loan is fully repaid (outstanding becomes zero or negative)
            if outstanding_for_this_row <= Decimal('0'):
                # Loan is fully repaid, no further interest calculation
                days = 0
            else:
                # Now calculate remaining period after repayment (exclusive)
                calculation_start_date = tx_date + timedelta(days=1)
                days = (end_date - calculation_start_date).days
                if is_last_tx_in_month: days += 1

                # Ensure days is not negative
                if days < 0:
                    days = 0
        else:
            # Standard calculation for non-inclusive or non-first-repayment transactions
            days = (end_date - tx_date).days
            if is_last_tx_in_month: days += 1

        # For simple interest, use 365 days consistently unless the calculation period includes Feb 29
        days_in_year = 365
        if is_leap(tx_date.year):
            # Check if the calculation period includes Feb 29
            start_date = tx_date
            end_date = tx_date + timedelta(days=days)
            feb_29 = datetime(tx_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366

        # SIMPLE INTEREST: Calculate interest on PRINCIPAL BALANCE ONLY (not accumulated balance)
        interest = (principal_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days)) / Decimal(days_in_year)

        # Calculate penalty interest for overdue scenarios (on principal balance)
        penalty_interest = Decimal('0')
        if customer.icl_end_date and tx_date > customer.icl_end_date:
            overdue_days_for_tx = calculate_overdue_days(customer, tx_date)
            if overdue_days_for_tx > 0:
                penalty_interest = calculate_penalty_interest(principal_balance, customer.penalty_rate, min(overdue_days_for_tx, days))

        total_interest = interest + penalty_interest
        net = total_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
        current_month_net_interest += net

        timeline.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(tx.paid),
            'received': Decimal(tx.received),
            'days': days, 
            'interest': interest,
            'penalty_interest': penalty_interest,
            'total_interest': total_interest,
            'net': net, 
            'tds': total_interest - net, 
            'outstanding': outstanding_for_this_row,
            'principal_balance': principal_balance,
            'is_overdue': customer.icl_end_date and tx_date > customer.icl_end_date
        })
        running_outstanding = outstanding_for_this_row

    final_outstanding = running_outstanding + current_month_net_interest
    last_tx_date = sorted_txs[-1].date
    last_tx_month_info = get_month_info(last_tx_date, customer.icl_start_date)
    timeline.append({'id': f"summary-{last_tx_month_info['name']}",'date': last_tx_month_info['endDate'],'description': f"{last_tx_month_info['name']} Net Interest",'paid': current_month_net_interest, 'received': Decimal('0'), 'isSummary': True,'outstanding': final_outstanding})

    return timeline

def calculate_yearly_compound_interest_data(customer, settings):
    """Calculate data using yearly compound interest logic based on financial year"""
    sorted_txs = sorted(customer.transactions, key=lambda x: x.date)

    timeline = []
    running_outstanding = Decimal('0')
    current_year_net_interest = Decimal('0')

    first_tx_date = sorted_txs[0].date
    last_year_info = get_financial_year_info(first_tx_date, customer.icl_start_date)

    def add_missing_years(from_year_info, to_year_info, outstanding_balance):
        """Add missing financial years between two years"""
        current_year = from_year_info

        while current_year['name'] != to_year_info['name']:
            # Calculate next financial year
            next_year_start = datetime(current_year['startDate'].year + 1, 4, 1).date()
            next_year_info = get_financial_year_info(next_year_start, customer.icl_start_date)

            if next_year_info['name'] == to_year_info['name']:
                break

            # Check if missing year end date exceeds ICL end date
            year_end_date = next_year_info['endDate']
            if customer.icl_end_date and year_end_date > customer.icl_end_date:
                year_end_date = customer.icl_end_date

            # Calculate interest for the missing year
            days_in_year_period = (year_end_date - current_year['endDate']).days
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(current_year['endDate'].year):
                # Check if the quarter period includes Feb 29
                start_date = current_year['endDate']
                end_date = year_end_date
                feb_29 = datetime(current_year['endDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366
            year_interest = (outstanding_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_year_period)) / Decimal(days_in_year)
            year_net = year_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Add missing year entry
            timeline.append({
                'id': f"missing-{next_year_info['name']}",
                'date': year_end_date,
                'description': f"{next_year_info['name']} Net Interest (No Transactions)",
                'paid': year_net,
                'received': Decimal('0'),
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + year_net,
                'interest': year_interest,
                'tds': year_interest - year_net,
                'net': year_net,
                'days': days_in_year_period
            })

            outstanding_balance += year_net
            current_year = next_year_info

            # Stop if we've reached ICL end date
            if customer.icl_end_date and year_end_date >= customer.icl_end_date:
                break

        return outstanding_balance

    for i, tx in enumerate(sorted_txs):
        tx_date = tx.date
        tx_year_info = get_financial_year_info(tx_date, customer.icl_start_date)

        # Check if this is the first transaction in a new year and if it's a repayment
        is_first_tx_in_year = tx_year_info['name'] != last_year_info['name']
        is_repayment = Decimal(tx.received) > Decimal('0')

        if is_first_tx_in_year:
            # Close previous financial year
            running_outstanding += current_year_net_interest
            timeline.append({'id': f"summary-{last_year_info['name']}",'date': last_year_info['endDate'],'description': f"{last_year_info['name']} Net Interest",'paid': current_year_net_interest, 'received': Decimal('0'), 'isSummary': True,'outstanding': running_outstanding})

            # Add missing years if any
            running_outstanding = add_missing_years(last_year_info, tx_year_info, running_outstanding)

            # Add opening balance for current financial year
            opening_end_date = tx_date
            if customer.icl_end_date and opening_end_date > customer.icl_end_date:
                opening_end_date = customer.icl_end_date

            days_opening = (opening_end_date - tx_year_info['startDate']).days
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_year_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_year_info['startDate']
                end_date = opening_end_date
                feb_29 = datetime(tx_year_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366
            interest_opening = (running_outstanding * (settings['interestRate'] / Decimal('100')) * Decimal(days_opening)) / Decimal(days_in_year_opening)
            net_opening = interest_opening * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            timeline.append({'id': f"opening-{tx_year_info['name']}",'date': tx_year_info['startDate'],'description': 'Opening Balance','paid': Decimal('0'), 'received': Decimal('0'), 'isOpening': True,'outstanding': running_outstanding,'days': days_opening,'interest': interest_opening,'tds': interest_opening - net_opening,'net': net_opening})

            current_year_net_interest = net_opening
            last_year_info = tx_year_info

        outstanding_for_this_row = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

        next_tx = sorted_txs[i + 1] if i + 1 < len(sorted_txs) else None
        end_date = tx_year_info['endDate']
        is_last_tx_in_year = True

        # Respect ICL end date constraint
        if customer.icl_end_date and end_date > customer.icl_end_date:
            end_date = customer.icl_end_date
            is_last_tx_in_year = True

        if next_tx:
            next_tx_date = next_tx.date
            next_tx_year_info = get_financial_year_info(next_tx_date, customer.icl_start_date)
            if next_tx_year_info['name'] == tx_year_info['name']:
                if not customer.icl_end_date or next_tx_date <= customer.icl_end_date:
                    end_date = next_tx_date
                    is_last_tx_in_year = False

        # Special condition for exclusive method: if first transaction is repayment on year end date, use inclusive logic
        use_inclusive_logic = False
        if customer.repayment_method == 'inclusive':
            use_inclusive_logic = True
        elif (customer.repayment_method == 'exclusive' and is_first_tx_in_year and 
              is_repayment and tx_date == tx_year_info['endDate']):
            use_inclusive_logic = True

        # For inclusive repayment method or special exclusive condition, adjust the calculation period
        if is_first_tx_in_year and is_repayment and use_inclusive_logic:
            # For inclusive method, calculate interest from year start to repayment date (including repayment date)
            opening_period_end = tx_date
            opening_days = (opening_period_end - tx_year_info['startDate']).days + 1  # +1 to include repayment date

            # Calculate the reduced outstanding balance after repayment
            reduced_outstanding_balance = running_outstanding + Decimal(tx.paid) - Decimal(tx.received)

            # Special condition: If repayment amount is larger than outstanding amount, 
            # use original outstanding balance for interest calculation
            balance_for_interest_calculation = reduced_outstanding_balance
            if Decimal(tx.received) > running_outstanding:
                balance_for_interest_calculation = running_outstanding

            # Calculate interest for the opening period on the appropriate balance (year start to repayment date inclusive)
            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year_opening = 365
            if is_leap(tx_year_info['startDate'].year):
                # Check if the opening period includes Feb 29
                start_date = tx_year_info['startDate']
                end_date = opening_period_end
                feb_29 = datetime(tx_year_info['startDate'].year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year_opening = 366
            opening_interest = (balance_for_interest_calculation * (settings['interestRate'] / Decimal('100')) * Decimal(opening_days)) / Decimal(days_in_year_opening)
            opening_net = opening_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            # Update the opening balance entry with correct interest calculation
            for entry in timeline:
                if entry.get('id') == f"opening-{tx_year_info['name']}":
                    entry['days'] = opening_days
                    entry['interest'] = opening_interest
                    entry['net'] = opening_net
                    entry['tds'] = opening_interest - opening_net
                    break

            # Update current year net interest
            current_year_net_interest = opening_net

            # Check if loan is fully repaid (outstanding becomes zero or negative)
            if outstanding_for_this_row <= Decimal('0'):
                # Loan is fully repaid, no further interest calculation
                days = 0
            else:
                # Now calculate remaining period after repayment (exclusive)
                calculation_start_date = tx_date + timedelta(days=1)
                days = (end_date - calculation_start_date).days
                if is_last_tx_in_year: days += 1

                # Ensure days is not negative
                if days < 0:
                    days = 0
        else:
            # Calculate days - for yearly compounding, include both start and end dates
            days = (end_date - tx_date).days
            if is_last_tx_in_year: 
                days += 1

        # For yearly compounding, use 365 days as standard practice unless it's a leap year calculation
        days_in_year = 365  # Standard year for interest calculation
        # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
        if is_leap(tx_date.year):
            # Check if the calculation period includes Feb 29
            start_date = tx_date
            end_date = tx_date + timedelta(days=days)
            feb_29 = datetime(tx_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366
        interest = (outstanding_for_this_row * (settings['interestRate'] / Decimal('100')) * Decimal(days)) / Decimal(days_in_year)

        # Calculate penalty interest for overdue scenarios
        penalty_interest = Decimal('0')
        if customer.icl_end_date and tx_date > customer.icl_end_date:
            overdue_days_for_tx = calculate_overdue_days(customer, tx_date)
            if overdue_days_for_tx > 0:
                penalty_interest = calculate_penalty_interest(outstanding_for_this_row, customer.penalty_rate, min(overdue_days_for_tx, days))

        total_interest = interest + penalty_interest
        net = total_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
        current_year_net_interest += net

        timeline.append({
            'id': tx.id,
            'date': tx.date,
            'description': tx.description,
            'paid': Decimal(tx.paid),
            'received': Decimal(tx.received),
            'days': days, 
            'interest': interest,
            'penalty_interest': penalty_interest,
            'total_interest': total_interest,
            'net': net, 
            'tds': total_interest - net, 
            'outstanding': outstanding_for_this_row,
            'is_overdue': customer.icl_end_date and tx_date > customer.icl_end_date
        })
        running_outstanding = outstanding_for_this_row

    # Close the current financial year
    final_outstanding = running_outstanding + current_year_net_interest
    last_tx_date = sorted_txs[-1].date
    last_tx_year_info = get_financial_year_info(last_tx_date, customer.icl_start_date)
    
    timeline.append({'id': f"summary-{last_tx_year_info['name']}",'date': last_tx_year_info['endDate'],'description': f"{last_tx_year_info['name']} Net Interest",'paid': current_year_net_interest, 'received': Decimal('0'), 'isSummary': True,'outstanding': final_outstanding})

    # Check if ICL end date extends beyond the current financial year
    if customer.icl_end_date and customer.icl_end_date > last_tx_year_info['endDate']:
        # Calculate interest for the remaining period from financial year end to ICL end date
        remaining_period_start = last_tx_year_info['endDate'] + timedelta(days=1)
        remaining_period_end = customer.icl_end_date
        remaining_days = (remaining_period_end - remaining_period_start).days + 1
        
        if remaining_days > 0:
            settings = {
                'interestRate': Decimal(customer.interest_rate),
                'tdsRate': Decimal(customer.tds_rate)
            }
            
            # Calculate days in year for the remaining period
            days_in_year = 365
            if is_leap(remaining_period_start.year):
                # Check if the remaining period includes Feb 29
                feb_29 = datetime(remaining_period_start.year, 2, 29).date()
                if remaining_period_start <= feb_29 <= remaining_period_end:
                    days_in_year = 366
            
            remaining_interest = (final_outstanding * (settings['interestRate'] / Decimal('100')) * Decimal(remaining_days)) / Decimal(days_in_year)
            remaining_net = remaining_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
            
            # Add the remaining period entry
            timeline.append({
                'id': f"remaining-period-{remaining_period_end.strftime('%Y-%m-%d')}",
                'date': remaining_period_end,
                'description': f"Remaining Period Interest (to ICL End Date)",
                'paid': remaining_net,
                'received': Decimal('0'),
                'isSummary': True,
                'outstanding': final_outstanding + remaining_net,
                'interest': remaining_interest,
                'tds': remaining_interest - remaining_net,
                'net': remaining_net,
                'days': remaining_days
            })
            
            final_outstanding += remaining_net

    return timeline


# --- Routes ---
@app.route('/')
@login_required
def index():
    customers = Customer.query.all()
    current_user = get_current_user()
    
    # Calculate dashboard statistics
    dashboard_stats = calculate_dashboard_stats(customers)
    
    # Get recent transactions (last 5)
    recent_transactions = get_recent_transactions_with_customer_names()
    
    return render_template('dashboard.html', 
                         customers=customers, 
                         current_user=current_user,
                         dashboard_stats=dashboard_stats,
                         recent_transactions=recent_transactions)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password) and user.is_active:
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            flash(f'Welcome back, {user.username}!', 'success')

            # Redirect to next page if specified, otherwise to index
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password.', 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        role = request.form.get('role', 'user')

        # Check if user already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('Email already exists.', 'error')
            return render_template('register.html')

        # Create new user
        new_user = User(username=username, email=email, role=role)
        new_user.set_password(password)

        db.session.add(new_user)
        db.session.commit()

        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/users')
@admin_required
def manage_users():
    users = User.query.all()
    current_user = get_current_user()
    return render_template('manage_users.html', users=users, current_user=current_user)

@app.route('/customers')
@login_required
def customers():
    customers = Customer.query.all()
    current_user = get_current_user()
    return render_template('customers.html', customers=customers, current_user=current_user)

@app.route('/customer/<int:customer_id>')
@login_required
def customer_detail(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    # No need to cast to Decimal here as it's handled in the calculation engine
    calculated_data = calculate_data(customer)
    current_user = get_current_user()
    return render_template('customer_detail.html', customer=customer, data=calculated_data, today=datetime.now().strftime('%Y-%m-%d'),datetime=datetime, current_user=current_user)

@app.route('/add_customer', methods=['GET', 'POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        name = request.form['name']
        address = request.form.get('address', '')
        icl_start_date = datetime.strptime(request.form['icl_start_date'], '%Y-%m-%d').date()
        icl_end_date = request.form.get('icl_end_date')
        if icl_end_date:
            icl_end_date = datetime.strptime(icl_end_date, '%Y-%m-%d').date()
        else:
            icl_end_date = None

        interest_rate = float(request.form['interest_rate'])

        # Handle TDS checkbox
        tds_enabled = 'tds_enabled' in request.form
        if tds_enabled and request.form.get('tds_rate'):
            tds_rate = float(request.form['tds_rate'])
        else:
            tds_rate = 0.0

        penalty_rate = float(request.form.get('penalty_rate', 2.0))
        grace_period = int(request.form.get('grace_period', 30))
        frequency = request.form['frequency']
        interest_type = request.form.get('interest_type', 'compound')
        repayment_method = request.form.get('repayment_method', 'exclusive')

        customer = Customer(
            name=name,
            address=address,
            icl_start_date=icl_start_date,
            icl_end_date=icl_end_date,
            interest_rate=interest_rate,
            tds_rate=tds_rate,
            penalty_rate=penalty_rate,
            grace_period=grace_period,
            frequency=frequency,
            interest_type=interest_type,
            repayment_method=repayment_method
        )

        db.session.add(customer)
        db.session.commit()
        flash('Customer added successfully!', 'success')
        return redirect(url_for('index'))

    current_user = get_current_user()
    return render_template('add_customer.html', current_user=current_user)

@app.route('/edit_customer/<int:customer_id>', methods=['GET', 'POST'])
@login_required
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if request.method == 'POST':
        customer.name = request.form['name']
        customer.address = request.form['address']
        customer.icl_start_date = datetime.strptime(request.form['icl_start_date'], '%Y-%m-%d').date()
        if request.form.get('icl_end_date'):
            customer.icl_end_date = datetime.strptime(request.form['icl_end_date'], '%Y-%m-%d').date()
        else:
            customer.icl_end_date = None
        customer.interest_rate = float(request.form['interest_rate'])
        # Handle optional TDS
        if request.form.get('tds_enabled'):
            customer.tds_rate = float(request.form.get('tds_rate', 0))
        else:
            customer.tds_rate = 0.0
        customer.interest_type = request.form['interest_type']
        customer.frequency = request.form['frequency']
        customer.penalty_rate = float(request.form.get('penalty_rate', customer.penalty_rate))
        customer.grace_period = int(request.form.get('grace_period', customer.grace_period))
        customer.repayment_method = request.form.get('repayment_method', customer.repayment_method)
        db.session.commit()
        flash(f"Customer '{customer.name}' updated successfully!", 'success')
        return redirect(url_for('customer_detail', customer_id=customer.id))
    current_user = get_current_user()
    return render_template('edit_customer.html', customer=customer, current_user=current_user)

@app.route('/delete_customer/<int:customer_id>', methods=['POST'])
def delete_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    db.session.delete(customer)
    db.session.commit()
    flash(f"Customer '{customer.name}' and all associated transactions have been deleted.", 'success')
    return redirect(url_for('index'))

@app.route('/customer/<int:customer_id>/add_transaction', methods=['POST'])
def add_transaction(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    transaction_date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()

    # Check if transaction date is beyond ICL end date
    if customer.icl_end_date and transaction_date > customer.icl_end_date:
        flash(f'Transaction date cannot be beyond the ICL end date ({customer.icl_end_date.strftime("%d/%m/%Y")})', 'error')
        return redirect(url_for('customer_detail', customer_id=customer.id))

    new_tx = Transaction(
        date=transaction_date,
        description=request.form['description'],
        paid=float(request.form.get('paid', 0) or 0),
        received=float(request.form.get('received', 0) or 0),
        customer_id=customer.id
    )
    db.session.add(new_tx)
    db.session.commit()
    flash('Transaction added successfully!', 'success')
    return redirect(url_for('customer_detail', customer_id=customer.id))

@app.route('/edit_transaction/<int:transaction_id>', methods=['GET', 'POST'])
@admin_required
def edit_transaction(transaction_id):
    tx = Transaction.query.get_or_404(transaction_id)
    customer = Customer.query.get_or_404(tx.customer_id)
    
    if request.method == 'POST':
        # Store old values for logging
        old_date = tx.date
        old_description = tx.description
        old_paid = tx.paid
        old_received = tx.received
        
        # Update transaction
        new_date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        
        # Check if transaction date is beyond ICL end date
        if customer.icl_end_date and new_date > customer.icl_end_date:
            flash(f'Transaction date cannot be beyond the ICL end date ({customer.icl_end_date.strftime("%d/%m/%Y")})', 'error')
            return redirect(url_for('edit_transaction', transaction_id=transaction_id))
        
        tx.date = new_date
        tx.description = request.form['description']
        tx.paid = float(request.form.get('paid', 0) or 0)
        tx.received = float(request.form.get('received', 0) or 0)
        
        db.session.commit()
        flash(f'Transaction updated successfully! All calculations have been recalculated.', 'success')
        return redirect(url_for('customer_detail', customer_id=customer.id))
    
    current_user = get_current_user()
    return render_template('edit_transaction.html', transaction=tx, customer=customer, current_user=current_user)

@app.route('/delete_transaction/<int:transaction_id>', methods=['POST'])
def delete_transaction(transaction_id):
    tx = Transaction.query.get_or_404(transaction_id)
    customer_id = tx.customer_id
    db.session.delete(tx)
    db.session.commit()
    flash('Transaction deleted successfully!', 'success')
    return redirect(url_for('customer_detail', customer_id=customer_id))

@app.route('/customer/<int:customer_id>/delete_all', methods=['POST'])
def delete_all_transactions(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    Transaction.query.filter_by(customer_id=customer.id).delete()
    db.session.commit()
    flash('All transactions for this customer have been deleted.', 'success')
    return redirect(url_for('customer_detail', customer_id=customer.id))

@app.route('/customer/<int:customer_id>/reports')
@login_required
def customer_reports(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    calculated_data = calculate_data(customer)
    current_user = get_current_user()
    return render_template('customer_reports.html', customer=customer, data=calculated_data, current_user=current_user)

@app.route('/customer/<int:customer_id>/close_loan', methods=['POST'])
def close_loan(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    closure_date = datetime.strptime(request.form['closure_date'], '%Y-%m-%d').date()

    # Calculate final settlement amount
    settlement_amount = calculate_settlement_amount(customer, closure_date)

    # Add final settlement transaction
    settlement_tx = Transaction(
        date=closure_date,
        description='Loan Closure - Final Settlement',
        paid=Decimal('0'),
        received=float(settlement_amount['total_settlement']),
        customer_id=customer.id
    )
    db.session.add(settlement_tx)

    # Update customer status
    customer.status = 'closed'
    customer.closure_date = closure_date
    customer.overdue_days = 0

    db.session.commit()
    flash(f'Loan closed successfully. Settlement amount: {settlement_amount["total_settlement"]:,.2f}', 'success')
    return redirect(url_for('customer_detail', customer_id=customer.id))

def calculate_settlement_amount(customer, closure_date):
    """Calculate final settlement amount including all dues and penalties"""
    calculated_data = calculate_data(customer)

    if not calculated_data:
        return {
            'outstanding_principal': Decimal('0'),
            'accrued_interest': Decimal('0'),
            'penalty_amount': Decimal('0'),
            'total_settlement': Decimal('0')
        }

    # Get last outstanding balance
    last_entry = calculated_data[-1]
    outstanding_balance = last_entry['outstanding']

    # Calculate additional interest/penalty from last transaction to closure date
    last_date = last_entry['date']
    additional_days = (closure_date - last_date).days

    additional_interest = Decimal('0')
    penalty_amount = Decimal('0')

    if additional_days > 0:
        settings = {
            'interestRate': Decimal(customer.interest_rate),
            'tdsRate': Decimal(customer.tds_rate)
        }

        # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
        days_in_year = 365
        if is_leap(closure_date.year):
            # Check if the calculation period includes Feb 29
            start_date = last_date
            end_date = closure_date
            feb_29 = datetime(closure_date.year, 2, 29).date()
            if start_date <= feb_29 <= end_date:
                days_in_year = 366
        additional_interest = (outstanding_balance * (settings['interestRate'] / Decimal('100')) * Decimal(additional_days)) / Decimal(days_in_year)

        # Calculate penalty if overdue
        if customer.icl_end_date and closure_date > customer.icl_end_date:
            overdue_days = calculate_overdue_days(customer, closure_date)
            if overdue_days > 0:
                penalty_amount = calculate_penalty_interest(outstanding_balance, customer.penalty_rate, min(overdue_days, additional_days))

    total_additional = additional_interest + penalty_amount
    net_additional = total_additional * (Decimal('1') - Decimal(customer.tds_rate) / Decimal('100'))

    total_settlement = outstanding_balance + net_additional

    return {
        'outstanding_principal': outstanding_balance,
        'accrued_interest': additional_interest,
        'penalty_amount': penalty_amount,
        'net_additional': net_additional,
        'total_settlement': total_settlement
    }

@app.route('/reports')
@login_required
def reports():
    """Main reports page"""
    current_user = get_current_user()
    return render_template('reports.html', current_user=current_user)

@app.route('/reports/customer-wise', methods=['GET', 'POST'])
@login_required
def customer_wise_report():
    """Customer wise report"""
    customers = Customer.query.all()
    report_data = None
    selected_customer = None
    
    if request.method == 'POST':
        customer_id = request.form.get('customer_id')
        if customer_id:
            selected_customer = Customer.query.get(int(customer_id))
            if selected_customer:
                calculated_data = calculate_data(selected_customer)
                current_balance = Decimal('0')
                if calculated_data:
                    current_balance = calculated_data[-1]['outstanding']
                
                report_data = {
                    'customer': selected_customer,
                    'current_balance': current_balance,
                    'calculated_data': calculated_data
                }
    
    current_user = get_current_user()
    return render_template('customer_wise_report.html', 
                         customers=customers, 
                         report_data=report_data,
                         selected_customer=selected_customer,
                         current_user=current_user,datetime=datetime)

@app.route('/reports/period-based', methods=['GET', 'POST'])
@login_required
def period_based_report():
    """Period based report for all customers"""
    report_data = []
    start_date = None
    end_date = None
    
    if request.method == 'POST':
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')
        
        if start_date_str and end_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            
            customers = Customer.query.all()
            
            for customer in customers:
                # Calculate totals for the period
                total_given = Decimal('0')
                total_repaid = Decimal('0')
                period_interest = Decimal('0')
                period_tds = Decimal('0')
                principal_balance = Decimal('0')
                
                # Get all transactions in the period
                period_transactions = Transaction.query.filter(
                    Transaction.customer_id == customer.id,
                    Transaction.date >= start_date,
                    Transaction.date <= end_date
                ).all()
                
                for tx in period_transactions:
                    total_given += Decimal(str(tx.paid))
                    total_repaid += Decimal(str(tx.received))
                
                # Calculate interest for the period using the calculation engine
                calculated_data = calculate_data(customer)
                
                for entry in calculated_data:
                    if start_date <= entry['date'] <= end_date:
                        if entry.get('interest'):
                            period_interest += entry['interest']
                        if entry.get('tds'):
                            period_tds += entry['tds']
                
                # Get current principal balance (total given - total repaid overall)
                all_transactions = Transaction.query.filter_by(customer_id=customer.id).all()
                total_given_overall = sum(Decimal(str(tx.paid)) for tx in all_transactions)
                total_repaid_overall = sum(Decimal(str(tx.received)) for tx in all_transactions)
                principal_balance = total_given_overall - total_repaid_overall
                
                # Calculate net amount (Principal + Interest - TDS)
                net_amount = principal_balance + period_interest - period_tds
                
                report_data.append({
                    'icl_manual_no': f"ICL-{customer.id:04d}",
                    'customer_name': customer.name,
                    'icl_start_date': customer.icl_start_date,
                    'icl_end_date': customer.icl_end_date,
                    'interest_rate': customer.interest_rate,
                    'total_given': total_given,
                    'total_repaid': total_repaid,
                    'principal_balance': principal_balance,
                    'period_interest': period_interest,
                    'period_tds': period_tds,
                    'net_amount': net_amount
                })
    
    current_user = get_current_user()
    return render_template('period_based_report.html',
                         report_data=report_data,
                         start_date=start_date,
                         end_date=end_date,
                         current_user=current_user)

@app.route('/customer/<int:customer_id>/balance_calculator', methods=['GET', 'POST'])
def calculate_balance(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    recent_transactions = Transaction.query.filter_by(customer_id=customer.id).order_by(Transaction.date.desc()).limit(5).all()

    balance_result = None
    selected_date = None

    if request.method == 'POST':
        calculation_date = datetime.strptime(request.form['calculation_date'], '%Y-%m-%d').date()
        selected_date = calculation_date.strftime('%Y-%m-%d')

        # Validate date range
        if calculation_date < customer.icl_start_date:
            flash('Calculation date cannot be before ICL start date.', 'error')
            return redirect(url_for('calculate_balance', customer_id=customer.id))

        if customer.icl_end_date and calculation_date > customer.icl_end_date:
            flash('Calculation date is beyond ICL end date. Balance will be calculated up to ICL end date.', 'warning')

        balance_result = calculate_balance_at_date(customer, calculation_date)

    current_user = get_current_user()
    return render_template('balance_calculator.html', 
                         customer=customer, 
                         balance_result=balance_result,
                         recent_transactions=recent_transactions,
                         selected_date=selected_date,
                         today=datetime.now().strftime('%Y-%m-%d'),
                         current_user=current_user)

def calculate_balance_at_date(customer, target_date):
    """Calculate the exact balance at a specific date using the same logic as the main calculation"""
    if not customer.transactions:
        # No transactions case - calculate simple interest from ICL start to target date
        effective_target_date = target_date
        is_beyond_icl_end = False
        if customer.icl_end_date and target_date > customer.icl_end_date:
            effective_target_date = customer.icl_end_date
            is_beyond_icl_end = True

        days_from_start = (effective_target_date - customer.icl_start_date).days
        # With no transactions, balance remains 0
        return {
            'calculation_date': target_date,
            'outstanding_balance': Decimal('0'),
            'total_paid': Decimal('0'),
            'total_received': Decimal('0'),
            'total_interest': Decimal('0'),
            'net_interest': Decimal('0'),
            'days_from_start': days_from_start,
            'transaction_count': 0,
            'last_transaction_date': None,
            'is_beyond_icl_end': is_beyond_icl_end,
            'is_predicted': False
        }

    # Respect ICL end date constraint
    effective_target_date = target_date
    is_beyond_icl_end = False
    if customer.icl_end_date and target_date > customer.icl_end_date:
        effective_target_date = customer.icl_end_date
        is_beyond_icl_end = True

    # Use the main calculation engine and filter results up to target date
    full_timeline = calculate_data(customer)

    # Find all entries up to and including the target date
    relevant_entries = []
    running_balance = Decimal('0')
    total_paid = Decimal('0')
    total_received = Decimal('0')
    total_interest = Decimal('0')
    net_interest = Decimal('0')
    last_transaction_date = None

    for entry in full_timeline:
        if entry['date'] <= effective_target_date:
            relevant_entries.append(entry)
            running_balance = entry['outstanding']

            # Track totals only for actual transactions (not summaries)
            if not entry.get('isSummary') and not entry.get('isOpening'):
                total_paid += entry['paid']
                total_received += entry['received']
                last_transaction_date = entry['date']

            # Track all interest
            if entry.get('interest'):
                total_interest += entry['interest']
            if entry.get('net'):
                net_interest += entry['net']

    # If target date is beyond last entry, we need to predict
    is_predicted = False
    if relevant_entries and effective_target_date > relevant_entries[-1]['date']:
        is_predicted = True
        last_entry = relevant_entries[-1]
        last_date = last_entry['date']
        last_balance = last_entry['outstanding']

        # Calculate additional interest from last entry date to target date
        additional_days = (effective_target_date - last_date).days
        if additional_days > 0:
            settings = {
                'interestRate': Decimal(customer.interest_rate),
                'tdsRate': Decimal(customer.tds_rate)
            }

            # For compound interest, use 365 days consistently unless the calculation period includes Feb 29
            days_in_year = 365
            if is_leap(last_date.year):
                # Check if the calculation period includes Feb 29
                start_date = last_date
                end_date = effective_target_date
                feb_29 = datetime(last_date.year, 2, 29).date()
                if start_date <= feb_29 <= end_date:
                    days_in_year = 366
            additional_interest = (last_balance * (settings['interestRate'] / Decimal('100')) * Decimal(additional_days)) / Decimal(days_in_year)
            additional_net_interest = additional_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))

            total_interest += additional_interest
            net_interest += additional_net_interest
            running_balance += additional_net_interest

    # Get last actual transaction date
    all_transactions = sorted(customer.transactions, key=lambda x: x.date)
    actual_last_transaction_date = all_transactions[-1].date if all_transactions else None

    # Determine if this is truly a prediction (beyond last actual transaction)
    is_predicted = effective_target_date > actual_last_transaction_date if actual_last_transaction_date else False

    return {
        'calculation_date': target_date,
        'outstanding_balance': running_balance,
        'total_paid': total_paid,
        'total_received': total_received,
        'total_interest': total_interest,
        'net_interest': net_interest,
        'days_from_start': (target_date - customer.icl_start_date).days,
        'transaction_count': len([tx for tx in customer.transactions if tx.date <= effective_target_date]),
        'last_transaction_date': actual_last_transaction_date,
        'is_beyond_icl_end': is_beyond_icl_end,
        'is_predicted': is_predicted
    }

@app.route('/export/<int:customer_id>')
def export_excel(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    data_to_export = calculate_data(customer)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = f"{customer.name} Transactions"
    headers = [
        'Date', 'Description', 'Amount Paid', 'Amount Received', 'Days', 
        'Outstanding', f"Interest @ {customer.interest_rate}%", 
        f"TDS @ {customer.tds_rate}%", 'Net Interest'
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    for row_data in data_to_export:
        row_to_append = [
            row_data['date'],
            row_data['description'],
            float(row_data['paid']),
            float(row_data['received']),
            '' if row_data.get('isSummary') else row_data.get('days', ''),
            float(row_data['outstanding']),
            '' if row_data.get('isSummary') else float(row_data.get('interest', 0)),
            '' if row_data.get('isSummary') else float(row_data.get('tds', 0)),
            '' if row_data.get('isSummary') else float(row_data.get('net', 0)),
        ]
        sheet.append(row_to_append)

    currency_format = '₹ #,##0.00'
    for col_letter in ['C', 'D', 'F', 'G', 'H', 'I']:
        for cell in sheet[col_letter]:
            cell.number_format = currency_format
    for cell in sheet['A']:
        cell.number_format = 'DD/MM/YYYY'
    for col in sheet.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2)
        sheet.column_dimensions[column].width = adjusted_width

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f'{customer.name}_transactions.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

def create_default_users():
    """Create default users if they don't exist"""
    if not User.query.filter_by(username='admin').first():
        admin_user = User(username='admin', email='admin@example.com', role='admin')
        admin_user.set_password('admin123')
        db.session.add(admin_user)

    if not User.query.filter_by(username='manager').first():
        manager_user = User(username='manager', email='manager@example.com', role='manager')
        manager_user.set_password('manager123')
        db.session.add(manager_user)

    if not User.query.filter_by(username='user').first():
        user_user = User(username='user', email='user@example.com', role='user')
        user_user.set_password('user123')
        db.session.add(user_user)

    db.session.commit()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        create_default_users()
    app.run(debug=True, host='0.0.0.0', port=5000)
