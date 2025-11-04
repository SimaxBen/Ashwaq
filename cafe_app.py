import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import datetime, date, time
from collections import defaultdict

# --- Page Configuration ---
st.set_page_config(
    page_title="Cafe Manager",
    page_icon="☕",
    layout="wide"
)

# --- Supabase Connection ---
@st.cache_resource
def init_supabase_client():
    """Connects to Supabase using credentials from st.secrets."""
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        return create_client(url, key)
    except Exception as e:
        st.error(f"Error connecting to Supabase: {e}")
        st.info("Please check your .streamlit/secrets.toml file.")
        return None

db = init_supabase_client()

if not db:
    st.stop()

# --- Helper Functions (The "Backend" Logic) ---

def get_today_range(selected_date=None):
    """Returns the start and end timestamps for the selected date, or today if None."""
    today = selected_date if selected_date else date.today()
    start_of_day = datetime.combine(today, time.min).isoformat()
    end_of_day = datetime.combine(today, time.max).isoformat()
    return start_of_day, end_of_day

def get_month_range(selected_date):
    """Returns the start and end timestamps for the selected month."""
    start_of_month = selected_date.replace(day=1)
    next_month_start = (start_of_month.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
    end_of_month = next_month_start - pd.Timedelta(days=1)
    
    start_iso = datetime.combine(start_of_month, time.min).isoformat()
    end_iso = datetime.combine(end_of_month, time.max).isoformat()
    # === FIX ===
    # start_of_month is already a 'date' object, so .date() call was incorrect.
    return start_iso, end_iso, start_of_month

@st.cache_data(ttl=60)
def calculate_menu_item_cost(menu_item_id):
    """Calculates the cost of goods for a single menu item from its recipe."""
    try:
        # Fetch the recipe and the cost of each stock item
        recipe_response = db.table('menu_item_recipe').select(
            'quantity_used, stock_items(cost_per_unit)'
        ).eq('menu_item_id', menu_item_id).execute()
        
        if not recipe_response.data:
            return 0
        
        total_cost = 0
        for ingredient in recipe_response.data:
            if ingredient.get('stock_items'):
                total_cost += ingredient['quantity_used'] * ingredient['stock_items']['cost_per_unit']
        return total_cost
    except Exception as e:
        st.error(f"Error calculating item cost: {e}")
        return 0

def process_daily_sales(server_id, sales_dict: dict, sales_date: date):
    """
    Processes a server's entire daily sales report for a specific date.
    1. Creates one 'orders' entry for the server with the specified date.
    2. For each item in sales_dict, creates one 'order_items' entry with the total quantity.
    3. Decrements stock based on recipe * total quantity.
    """
    try:
        # 1. Create the single "order" for the day, with the correct date
        # Use noon on the selected date as a safe default timestamp
        sales_timestamp = datetime.combine(sales_date, time(12, 0)).isoformat()
        order_response = db.table('orders').insert({
            'server_id': server_id,
            'timestamp': sales_timestamp # Explicitly set the timestamp
        }).execute()
        
        if not order_response.data:
            st.error("Failed to create daily order.")
            return 0
        
        order_id = order_response.data[0]['id']
        total_revenue = 0
        
        # 2. Process each menu item's total quantity
        for item_id, details in sales_dict.items():
            quantity = details['quantity']
            if quantity == 0:
                continue

            price_at_sale = details['sale_price']
            
            # Calculate cost of goods for ONE item
            cost_at_sale_per_item = calculate_menu_item_cost(item_id)
            
            # Insert into order_items with the total quantity
            db.table('order_items').insert({
                'order_id': order_id,
                'menu_item_id': item_id,
                'quantity': quantity,
                'price_at_sale': price_at_sale, # Price for 1 item
                'cost_at_sale': cost_at_sale_per_item # Cost for 1 item
            }).execute()
            
            total_revenue += price_at_sale * quantity
            
            # 3. Decrement stock based on recipe
            recipe_response = db.table('menu_item_recipe').select(
                'stock_item_id, quantity_used'
            ).eq('menu_item_id', item_id).execute()
            
            for ingredient in recipe_response.data:
                total_amount_to_reduce = ingredient['quantity_used'] * quantity
                
                # Call the Supabase database function
                db.rpc('decrement_stock', {
                    'item_id': ingredient['stock_item_id'],
                    'amount_to_reduce': total_amount_to_reduce
                }).execute()
                
        return total_revenue
    except Exception as e:
        st.error(f"Error processing sales: {e}")
        return 0

# --- UI Rendering Functions (The "Frontend") ---

def render_monthly_dashboard():
    """Main dashboard showing current month's profit."""
    st.title("☕ Monthly Dashboard")
    st.header(f"Profit Report for {date.today().strftime('%B %Y')}")

    selected_month_date = date.today()
    start_month_iso, end_month_iso, month_start_date = get_month_range(selected_month_date)
    
    try:
        # 1. Get Revenue and COGS for the month
        sales_data = db.table('order_items').select(
            'price_at_sale, cost_at_sale, quantity, orders!inner(timestamp)'
        ).gte('orders.timestamp', start_month_iso).lte('orders.timestamp', end_month_iso).execute().data
        
        total_revenue = sum(item['price_at_sale'] * item['quantity'] for item in sales_data)
        total_cogs = sum(item['cost_at_sale'] * item['quantity'] for item in sales_data)
        gross_profit = total_revenue - total_cogs
        
        # 2. Get Salaries
        salary_data = db.table('workers').select('salary').execute().data
        total_salaries = sum(item['salary'] for item in salary_data)
        
        # 3. Get Other Expenses for the month
        expense_data = db.table('monthly_expenses').select('amount').eq(
            'month', month_start_date.isoformat()
        ).execute().data
        total_expenses = sum(item['amount'] for item in expense_data)
        
        # 4. Calculate Net Profit
        total_costs_operating = total_salaries + total_expenses
        net_profit = gross_profit - total_costs_operating
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Revenue", f"${total_revenue:.2f}")
        col2.metric("Gross Profit (Revenue - COGS)", f"${gross_profit:.2f}")
        col3.metric("Net Profit", f"${net_profit:.2f}", delta_color=("inverse" if net_profit < 0 else "normal"))

        with st.expander("See Profit Breakdown"):
            st.markdown(f"""
            - **Total Revenue:** `{total_revenue:,.2f}`
            - **Total Cost of Goods (COGS):** `({total_cogs:,.2f})`
            - **Gross Profit:** `{gross_profit:,.2f}`
            ---
            - **Staff Salaries:** `({total_salaries:,.2f})`
            - **Other Expenses:** `({total_expenses:,.2f})`
            - **Total Operating Costs:** `({total_costs_operating:,.2f})`
            ---
            - **NET PROFIT:** `{net_profit:,.2f}`
            """)
            
    except Exception as e:
        st.error(f"Error generating monthly report: {e}")

def render_daily_sales():
    """Page to enter the end-of-day sales for a server."""
    st.title("📝 Record End of Day Sales")
    st.info("Select a server, a date, and enter the *total quantity* of each item sold.")
    
    try:
        # Fetch data for forms
        servers = db.table('workers').select('id, name').eq('role', 'server').execute().data
        menu_items = db.table('menu_items').select('id, name, sale_price').order('name').execute().data
        
        if not servers:
            st.warning("No servers found. Please add a 'server' in the 'Staff' page.")
            return
            
        if not menu_items:
            st.warning("No menu items found. Please add items in the 'Menu' page.")
            return

        col1, col2 = st.columns(2)
        with col1:
            selected_server = st.selectbox(
                "Select Server",
                servers,
                format_func=lambda x: x['name']
            )
        with col2:
            selected_date = st.date_input("Select Sales Date", date.today())
        
        with st.form("daily_sales_form"):
            st.header(f"Sales for {selected_server['name']} on {selected_date.strftime('%Y-%m-%d')}")
            
            # Use session state to hold quantities
            sales_dict = {}
            
            cols = st.columns(3)
            col_index = 0
            
            for item in menu_items:
                with cols[col_index % 3]:
                    quantity = st.number_input(
                        f"Qty of {item['name']} (${item['sale_price']})", 
                        min_value=0, 
                        step=1, 
                        key=f"qty_{item['id']}"
                    )
                    if quantity > 0:
                        sales_dict[item['id']] = {
                            "quantity": quantity,
                            "sale_price": item['sale_price']
                        }
                col_index += 1

            submitted = st.form_submit_button("Submit Daily Sales", type="primary", use_container_width=True)
            if submitted:
                if selected_server and sales_dict and selected_date:
                    total_revenue = process_daily_sales(selected_server['id'], sales_dict, selected_date)
                    if total_revenue > 0:
                        st.success(f"Successfully recorded sales for {selected_server['name']} on {selected_date}. Total revenue: ${total_revenue:.2f}")
                        st.info("Stock has been updated.")
                    else:
                        st.error("There was an error processing the sales.")
                else:
                    st.warning("Please select a server, a date, and enter at least one item quantity.")
                
    except Exception as e:
        st.error(f"An error occurred: {e}")

def render_stock_management():
    """Page for viewing, adding, and restocking stock items."""
    st.title("📦 Stock Management")
    
    tab1, tab2, tab3 = st.tabs(["View & Delete Stock", "Add New Stock Item", "Restock"])
    
    # Fetch current stock for all tabs
    try:
        stock_data = db.table('stock_items').select('*').order('name').execute().data
    except Exception as e:
        st.error(f"Failed to load stock: {e}")
        return

    with tab1:
        st.header("Current Inventory")
        st.info("Expand any item to see details or delete it.")
        
        if not stock_data:
            st.warning("No stock items found.")
            # We don't return here, so tab2 and tab3 can still render
        else:
            for item in stock_data:
                color = ""
                if (item['tracking_type'] in ['UNIT', 'MULTI-USE'] and item['current_quantity'] < 10) or \
                   (item['tracking_type'] == 'MANUAL' and item['current_quantity'] == 0):
                    color = "red"

                label = f":{color}[{item['name']}] (Current: {item['current_quantity']} {item['unit_of_measure']})"
                
                with st.expander(label):
                    st.write(f"**Tracking Type:** {item['tracking_type']}")
                    st.write(f"**Cost Per Unit:** ${item['cost_per_unit']:.4f}")
                    st.write(f"**Item ID:** `{item['id']}`")
                    
                    if st.button("Delete This Item", key=f"del_stock_{item['id']}", type="primary"):
                        try:
                            # Check if item is in a recipe
                            recipe_links = db.table('menu_item_recipe').select('id').eq('stock_item_id', item['id']).execute().data
                            if recipe_links:
                                st.error(f"Cannot delete '{item['name']}'. It is used in {len(recipe_links)} recipe(s). Please remove it from all recipes first.")
                            else:
                                db.table('stock_items').delete().eq('id', item['id']).execute()
                                st.success(f"Deleted {item['name']}.")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting item: {e}")

    with tab2:
        st.header("Add New Stock Item")
        with st.form("new_stock_item_form"):
            name = st.text_input("Item Name (e.g., 'Coffee Beans', 'Bottle of Coke', 'Napkins')")
            tracking_type = st.selectbox(
                "Tracking Type",
                options=['UNIT', 'MULTI-USE', 'MANUAL'],
                help="""
                - **UNIT**: Tracked one-by-one (e.g., bottles, cans).
                - **MULTI-USE**: Used in portions (e.g., coffee beans, milk).
                - **MANUAL**: Tracked when it runs out (e.g., napkins, cleaning spray).
                """
            )
            current_quantity = st.number_input("Initial Quantity", min_value=0.0, step=1.0)
            unit_of_measure = st.text_input("Unit of Measure (e.g., 'g', 'ml', 'pcs', 'pack')")
            cost_per_unit = st.number_input("Cost Per Unit (Your cost, e.g., 0.02 for 1g coffee)", min_value=0.0, format="%.4f")
            
            submitted = st.form_submit_button("Add Item")
            if submitted:
                # === FIX ===
                # Changed validation to only check for 'name', as other fields
                # have valid defaults (like 0 for cost).
                if not name:
                    st.warning("Please fill out the 'Item Name'.")
                else:
                    try:
                        db.table('stock_items').insert({
                            'name': name,
                            'tracking_type': tracking_type,
                            'current_quantity': current_quantity,
                            'unit_of_measure': unit_of_measure,
                            'cost_per_unit': cost_per_unit
                        }).execute()
                        st.success(f"Added {name} to stock!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error adding item: {e}")

    with tab3:
        st.header("Restock Item")
        
        if not stock_data:
            st.warning("No stock items to restock.")
            return

        st.info("Add to existing stock quantity or mark a 'MANUAL' item as restocked.")
        
        item_to_restock = st.selectbox(
            "Select Item to Restock",
            stock_data,
            format_func=lambda x: f"{x['name']} (Current: {x['current_quantity']} {x['unit_of_measure']})"
        )
        
        if item_to_restock:
            if item_to_restock['tracking_type'] in ['UNIT', 'MULTI-USE']:
                amount_to_add = st.number_input("Quantity to Add", min_value=0.0, step=1.0)
                if st.button("Add to Stock"):
                    new_quantity = item_to_restock['current_quantity'] + amount_to_add
                    try:
                        db.table('stock_items').update(
                            {'current_quantity': new_quantity}
                        ).eq('id', item_to_restock['id']).execute()
                        st.success(f"Restocked {item_to_restock['name']}. New quantity: {new_quantity}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error restocking: {e}")
            else: # MANUAL tracking
                if st.button(f"Mark '{item_to_restock['name']}' as RESTOCKED"):
                    try:
                        # For manual items, we just set quantity to 1 (meaning "in stock")
                        db.table('stock_items').update(
                            {'current_quantity': 1}
                        ).eq('id', item_to_restock['id']).execute()
                        st.success(f"Marked {item_to_restock['name']} as restocked.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error restocking: {e}")

def render_menu_management():
    """Page for managing menu items and their recipes."""
    st.title("📋 Menu & Recipe Management")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.header("Add New Menu Item")
        with st.form("new_menu_item_form"):
            name = st.text_input("Menu Item Name (e.g., 'Latte')")
            sale_price = st.number_input("Sale Price ($)", min_value=0.0, step=0.01)
            submitted = st.form_submit_button("Add Menu Item")
            
            if submitted and name and sale_price > 0:
                try:
                    db.table('menu_items').insert({
                        'name': name,
                        'sale_price': sale_price
                    }).execute()
                    st.success(f"Added {name} to menu.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error adding item: {e}")
    
    with col2:
        st.header("View & Delete Menu Items")
        try:
            menu_data = db.table('menu_items').select('*').order('name').execute().data
            if not menu_data:
                st.warning("No menu items added yet.")
            else:
                for item in menu_data:
                    with st.expander(f"{item['name']} - ${item['sale_price']}"):
                        if st.button("Delete This Menu Item", key=f"del_menu_{item['id']}", type="primary"):
                            try:
                                # Must delete recipe links first
                                db.table('menu_item_recipe').delete().eq('menu_item_id', item['id']).execute()
                                # Then delete the item itself
                                db.table('menu_items').delete().eq('id', item['id']).execute()
                                st.success(f"Deleted {item['name']}.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error deleting: {e}")
                            
        except Exception as e:
            st.error(f"Failed to load menu: {e}")

    st.divider()
    
    st.header("Define Menu Recipe")
    st.info("Link what you sell (e.g., 'Latte') to what you have in stock (e.g., 'Coffee Beans').")
    
    try:
        menu_data = db.table('menu_items').select('id, name').execute().data
        stock_data = db.table('stock_items').select('id, name, unit_of_measure').execute().data
        
        if not menu_data or not stock_data:
            st.warning("Please add menu items and stock items first.")
            return

        col1, col2, col3 = st.columns(3)
        with col1:
            menu_item = st.selectbox(
                "Select Menu Item",
                menu_data,
                format_func=lambda x: x['name'],
                key="recipe_menu_item"
            )
        with col2:
            stock_item = st.selectbox(
                "Select Stock Ingredient",
                stock_data,
                format_func=lambda x: f"{x['name']} ({x['unit_of_measure']})",
                key="recipe_stock_item"
            )
        with col3:
            unit = next((item['unit_of_measure'] for item in stock_data if item['id'] == stock_item['id']), 'units')
            quantity_used = st.number_input(f"Quantity Used ({unit})", min_value=0.0, step=0.1, key="recipe_qty")
            
        if st.button("Add Ingredient to Recipe", use_container_width=True):
            if menu_item and stock_item and quantity_used > 0:
                try:
                    db.table('menu_item_recipe').insert({
                        'menu_item_id': menu_item['id'],
                        'stock_item_id': stock_item['id'],
                        'quantity_used': quantity_used
                    }).execute()
                    st.success(f"Added {quantity_used} {unit} of {stock_item['name']} to {menu_item['name']} recipe.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error adding to recipe: {e}")
                    
        # View current recipe for selected item
        if menu_item:
            recipe = db.table('menu_item_recipe').select(
                'id, quantity_used, stock_items(name, unit_of_measure)'
            ).eq('menu_item_id', menu_item['id']).execute().data
            
            if recipe:
                st.subheader(f"Recipe for {menu_item['name']}")
                for r in recipe:
                    if r.get('stock_items'):
                        col1, col2 = st.columns([4,1])
                        col1.write(f"- {r['quantity_used']} {r['stock_items']['unit_of_measure']} of {r['stock_items']['name']}")
                        if col2.button("Remove", key=f"del_recipe_{r['id']}", use_container_width=True):
                            db.table('menu_item_recipe').delete().eq('id', r['id']).execute()
                            st.rerun()
            
    except Exception as e:
        st.error(f"Error loading recipe data: {e}")


def render_staff_and_expenses():
    """Page for managing workers and monthly expenses."""
    st.title("👥 Staff & 🧾 Expenses")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.header("Manage Staff")
        with st.form("new_worker_form"):
            name = st.text_input("Worker Name")
            role = st.selectbox("Role", ["server", "barista"])
            salary = st.number_input("Monthly Salary ($)", min_value=0.0, step=50.0)
            submitted = st.form_submit_button("Add Worker")
            
            if submitted and name and role and salary >= 0:
                try:
                    db.table('workers').insert({
                        'name': name,
                        'role': role,
                        'salary': salary
                    }).execute()
                    st.success(f"Added {name}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error adding worker: {e}")
        
        st.subheader("Current Staff")
        try:
            staff_data = db.table('workers').select('id, name, role, salary').execute().data
            if not staff_data:
                st.warning("No staff added yet.")
            else:    
                for worker in staff_data:
                    with st.expander(f"{worker['name']} ({worker['role']}) - ${worker['salary']}/month"):
                        if st.button("Delete Worker", key=f"del_worker_{worker['id']}", type="primary"):
                            try:
                                # Check if worker has orders
                                orders = db.table('orders').select('id').eq('server_id', worker['id']).execute().data
                                if orders:
                                    st.error(f"Cannot delete {worker['name']}. They are linked to {len(orders)} order(s).")
                                else:
                                    db.table('workers').delete().eq('id', worker['id']).execute()
                                    st.success(f"Deleted {worker['name']}.")
                                    st.rerun()
                            except Exception as e:
                                st.error(f"Error deleting: {e}")
        except Exception as e:
            st.error(f"Failed to load staff: {e}")

    with col2:
        st.header("Manage Monthly Expenses")
        with st.form("new_expense_form"):
            month = st.date_input("Month", date.today().replace(day=1))
            description = st.text_input("Description (e.g., 'Electricity', 'Rent')")
            amount = st.number_input("Amount ($)", min_value=0.0, step=1.0)
            submitted = st.form_submit_button("Add Expense")
            
            if submitted and month and description and amount > 0:
                try:
                    db.table('monthly_expenses').insert({
                        'month': month.isoformat(),
                        'description': description,
                        'amount': amount
                    }).execute()
                    st.success(f"Added {description} expense.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error adding expense: {e}")

        st.subheader("Logged Expenses")
        try:
            expense_data = db.table('monthly_expenses').select('id, month, description, amount').order('month', desc=True).execute().data
            if not expense_data:
                st.warning("No expenses logged yet.")
            else:
                for expense in expense_data:
                    with st.expander(f"{expense['month']} - {expense['description']} - ${expense['amount']}"):
                        if st.button("Delete Expense", key=f"del_exp_{expense['id']}", type="primary"):
                            try:
                                db.table('monthly_expenses').delete().eq('id', expense['id']).execute()
                                st.success(f"Deleted {expense['description']}.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error deleting: {e}")
        except Exception as e:
            st.error(f"Failed to load expenses: {e}")

def render_reports():
    """Page for viewing profit reports."""
    st.title("📈 Profit Reports")
    
    report_type = st.radio("Select Report Type", ["Daily", "Monthly"], horizontal=True)
    
    if report_type == "Daily":
        st.subheader("Daily Profit Report")
        selected_date = st.date_input("Select Date", date.today())
        
        start_day, end_day = get_today_range(selected_date)
        
        sales_data = db.table('order_items').select(
            'price_at_sale, cost_at_sale, quantity, orders!inner(timestamp)'
        ).gte('orders.timestamp', start_day).lte('orders.timestamp', end_day).execute().data
        
        total_revenue = sum(item['price_at_sale'] * item['quantity'] for item in sales_data)
        total_cogs = sum(item['cost_at_sale'] * item['quantity'] for item in sales_data)
        gross_profit = total_revenue - total_cogs
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Revenue", f"${total_revenue:.2f}")
        col2.metric("Total Cost of Goods", f"${total_cogs:.2f}")
        col3.metric("Gross Profit", f"${gross_profit:.2f}")

    if report_type == "Monthly":
        st.subheader("Monthly Profit Report")
        selected_month_date = st.date_input("Select Month", date.today())
        
        start_month_iso, end_month_iso, month_start_date = get_month_range(selected_month_date)
        
        try:
            # 1. Get Revenue and COGS for the month
            sales_data = db.table('order_items').select(
                'price_at_sale, cost_at_sale, quantity, orders!inner(timestamp)'
            ).gte('orders.timestamp', start_month_iso).lte('orders.timestamp', end_month_iso).execute().data
            
            total_revenue = sum(item['price_at_sale'] * item['quantity'] for item in sales_data)
            total_cogs = sum(item['cost_at_sale'] * item['quantity'] for item in sales_data)
            gross_profit = total_revenue - total_cogs
            
            # 2. Get Salaries
            salary_data = db.table('workers').select('salary').execute().data
            total_salaries = sum(item['salary'] for item in salary_data)
            
            # 3. Get Other Expenses for the month
            expense_data = db.table('monthly_expenses').select('amount').eq(
                'month', month_start_date.isoformat()
            ).execute().data
            total_expenses = sum(item['amount'] for item in expense_data)
            
            # 4. Calculate Net Profit
            total_costs_operating = total_salaries + total_expenses
            net_profit = gross_profit - total_costs_operating
            
            st.subheader(f"Report for {selected_month_date.strftime('%B %Y')}")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Revenue", f"${total_revenue:.2f}")
            col2.metric("Gross Profit (Revenue - COGS)", f"${gross_profit:.2f}")
            col3.metric("Net Profit", f"${net_profit:.2f}", delta_color=("inverse" if net_profit < 0 else "normal"))

            with st.expander("See Profit Breakdown"):
                st.markdown(f"""
                - **Total Revenue:** `{total_revenue:,.2f}`
                - **Total Cost of Goods (COGS):** `({total_cogs:,.2f})`
                - **Gross Profit:** `{gross_profit:,.2f}`
                ---
                - **Staff Salaries:** `({total_salaries:,.2f})`
                - **Other Expenses:** `({total_expenses:,.2f})`
                - **Total Operating Costs:** `({total_costs_operating:,.2f})`
                ---
                - **NET PROFIT:** `{net_profit:,.2f}`
                """)
                
        except Exception as e:
            st.error(f"Error generating monthly report: {e}")

def render_manage_orders():
    """Page to view and delete past daily sales orders."""
    st.title("🛒 Manage Daily Orders")
    st.info("Here you can review and delete entire daily sales reports. Deleting an order will remove it from all profit calculations. It will NOT restock the items.")

    try:
        # Fetch all orders, joining with workers to get server name
        orders = db.table('orders').select(
            'id, timestamp, workers(name)'
        ).order('timestamp', desc=True).execute().data

        if not orders:
            st.warning("No orders found.")
            return

        for order in orders:
            server_name = order['workers']['name'] if order.get('workers') else "Unknown Server"
            order_time = datetime.fromisoformat(order['timestamp']).strftime('%Y-%m-%d %I:%M %p')
            
            with st.expander(f"**{server_name}**'s report from **{order_time}**"):
                
                # Fetch all items for this order
                items = db.table('order_items').select(
                    'quantity, price_at_sale, cost_at_sale, menu_items(name)'
                ).eq('order_id', order['id']).execute().data

                if items:
                    item_data = []
                    total_revenue = 0
                    total_cost = 0
                    for item in items:
                        if item.get('menu_items'):
                            item_name = item['menu_items']['name']
                            revenue = item['quantity'] * item['price_at_sale']
                            cost = item['quantity'] * item['cost_at_sale']
                            item_data.append({
                                "Item": item_name,
                                "Quantity": item['quantity'],
                                "Unit Price": f"${item['price_at_sale']:.2f}",
                                "Total Revenue": f"${revenue:.2f}",
                                "Total Cost": f"${cost:.2f}"
                            })
                            total_revenue += revenue
                            total_cost += cost
                    
                    st.dataframe(pd.DataFrame(item_data), hide_index=True, use_container_width=True)
                    st.markdown(f"**Total Revenue:** `${total_revenue:.2f}` | **Total Cost:** `${total_cost:.2f}`")

                else:
                    st.write("This order has no items.")

                if st.button("Delete This Entire Order", key=f"del_order_{order['id']}", type="primary"):
                    try:
                        # Deleting the order will cascade and delete all associated order_items
                        db.table('orders').delete().eq('id', order['id']).execute()
                        st.success(f"Deleted order from {order_time}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error deleting order: {e}")
                        
    except Exception as e:
        st.error(f"Error loading orders: {e}")


# --- Main Application Function ---
def main_app():
    """Renders the main application UI *after* successful login."""
    
    st.sidebar.title("Cafe Manager")
    
    # Add a logout button to the sidebar
    if st.sidebar.button("Logout"):
        st.session_state.logged_in = False
        st.rerun()

    page = st.sidebar.radio(
        "Navigation",
        ["Monthly Dashboard", "Record Daily Sales", "Manage Orders", "Stock Management", "Menu Management", "Staff & Expenses", "Reports"]
    )

    if page == "Monthly Dashboard":
        render_monthly_dashboard()
    elif page == "Record Daily Sales":
        render_daily_sales()
    elif page == "Manage Orders":
        render_manage_orders()
    elif page == "Stock Management":
        render_stock_management()
    elif page == "Menu Management":
        render_menu_management()
    elif page == "Staff & Expenses":
        render_staff_and_expenses()
    elif page == "Reports":
        render_reports()

# --- Login Page Function ---
def show_login_page():
    """Renders the login form."""
    st.title("☕ Cafe Manager Login")
    
    # Get credentials from secrets
    try:
        app_username = st.secrets["APP_USERNAME"]
        app_password = st.secrets["APP_PASSWORD"]
    except KeyError:
        st.error("Login credentials not found in secrets.toml.")
        st.info("Please add [APP_USERNAME] and [APP_PASSWORD] to your .streamlit/secrets.toml file.")
        st.stop()

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

        if submitted:
            if username == app_username and password == app_password:
                st.session_state.logged_in = True
                st.rerun()
            else:
                st.error("Invalid username or password")

# --- Main Control Flow ---
# Initialize session state for login
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

# Check login status and show appropriate page
if st.session_state.logged_in:
    main_app() # Run the main application
else:
    show_login_page() # Show the login form

