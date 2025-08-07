import os
import io
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
import openpyxl
from openpyxl.styles import Font, Alignment
from flask import send_file

# --- App & Database Configuration ---
app = Flask(__name__)
app.secret_key = os.urandom(24)
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

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    paid = db.Column(db.Float, nullable=False, default=0.0)
    received = db.Column(db.Float, nullable=False, default=0.0)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)

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

# --- Helper Functions ---
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
            days_in_year = 366 if is_leap(current_quarter['endDate'].year) else 365
            quarter_interest = (outstanding_balance * (settings['interestRate'] / Decimal('100')) * Decimal(days_in_quarter)) / Decimal(days_in_year)
            quarter_net = quarter_interest * (Decimal('1') - settings['tdsRate'] / Decimal('100'))
            
            # Add missing quarter entry
            timeline.append({
                'id': f"missing-{next_quarter_info['name']}",
                'date': quarter_end_date,
                'description': f"{next_quarter_info['name']} Net Interest (No Transactions)",
                'paid': quarter_net,
                'received': Decimal('0'),
                'days': days_in_quarter,
                'interest': quarter_interest,
                'tds': quarter_interest - quarter_net,
                'net': quarter_net,
                'isSummary': True,
                'isMissing': True,
                'outstanding': outstanding_balance + quarter_net
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

        if tx_quarter_info['name'] != last_quarter_info['name']:
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
            days_in_year_opening = 366 if is_leap(tx_quarter_info['startDate'].year) else 365
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

        days = (end_date - tx_date).days
        if is_last_tx_in_quarter: days += 1

        days_in_year = 366 if is_leap(tx_date.year) else 365
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

# --- Routes ---
@app.route('/')
def index():
    customers = Customer.query.all()
    return render_template('index.html', customers=customers)

@app.route('/customers')
def customers():
    customers = Customer.query.all()
    return render_template('customers.html', customers=customers)

@app.route('/customer/<int:customer_id>')
def customer_detail(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    calculated_data = calculate_data(customer)
    return render_template('customer_detail.html', customer=customer, data=calculated_data, today=datetime.now().strftime('%Y-%m-%d'),datetime=datetime)

@app.route('/add_customer', methods=['GET', 'POST'])
def add_customer():
    if request.method == 'POST':
        icl_end_date = None
        if request.form.get('icl_end_date'):
            icl_end_date = datetime.strptime(request.form['icl_end_date'], '%Y-%m-%d').date()
        
        new_customer = Customer(
            name=request.form['name'],
            address=request.form['address'],
            icl_start_date=datetime.strptime(request.form['icl_start_date'], '%Y-%m-%d').date(),
            icl_end_date=icl_end_date,
            interest_rate=float(request.form['interest_rate']),
            tds_rate=float(request.form['tds_rate']),
            interest_type=request.form['interest_type'],
            frequency=request.form['frequency'],
            penalty_rate=float(request.form.get('penalty_rate', 2.0)),
            grace_period=int(request.form.get('grace_period', 30))
        )
        db.session.add(new_customer)
        db.session.commit()
        flash(f"Customer '{new_customer.name}' added successfully!", 'success')
        return redirect(url_for('index'))
    return render_template('add_customer.html')

@app.route('/edit_customer/<int:customer_id>', methods=['GET', 'POST'])
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
        customer.tds_rate = float(request.form['tds_rate'])
        customer.interest_type = request.form['interest_type']
        customer.frequency = request.form['frequency']
        customer.penalty_rate = float(request.form.get('penalty_rate', customer.penalty_rate))
        customer.grace_period = int(request.form.get('grace_period', customer.grace_period))
        db.session.commit()
        flash(f"Customer '{customer.name}' updated successfully!", 'success')
        return redirect(url_for('customer_detail', customer_id=customer.id))
    return render_template('edit_customer.html', customer=customer)

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
def customer_reports(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    calculated_data = calculate_data(customer)
    return render_template('customer_reports.html', customer=customer, data=calculated_data)

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
        
        days_in_year = 366 if is_leap(closure_date.year) else 365
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
    
    return render_template('balance_calculator.html', 
                         customer=customer, 
                         balance_result=balance_result,
                         recent_transactions=recent_transactions,
                         selected_date=selected_date,
                         today=datetime.now().strftime('%Y-%m-%d'))

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
            
            days_in_year = 366 if is_leap(last_date.year) else 365
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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True,port=5000)
