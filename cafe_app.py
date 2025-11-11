import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import datetime, date, time
from collections import defaultdict
import calendar # Required for daily salary calculation

# --- Page Configuration ---
st.set_page_config(
    page_title="مدير المقهى",
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
        st.error(f"خطأ في الاتصال بـ Supabase: {e}")
        st.info("يرجى التحقق من ملف .streamlit/secrets.toml الخاص بك.")
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
    return start_iso, end_iso, start_of_month

@st.cache_data(ttl=60)
def get_daily_salary_cost(selected_date: date):
    """Fetches the total salary cost for a *specific* day using salary_history."""
    total_salaries_today = 0
    try:
        workers = db.table('workers').select('id').execute().data
        for worker in workers:
            # Get the most recent salary that started *on or before* the selected date
            salary_entry = db.table('salary_history').select('daily_salary').eq('worker_id', worker['id']).lte('start_date', selected_date.isoformat()).order('start_date', desc=True).limit(1).execute().data
            if salary_entry:
                total_salaries_today += salary_entry[0]['daily_salary']
        return total_salaries_today
    except Exception as e:
        st.error(f"خطأ في حساب رواتب اليوم: {e}")
        return 0

@st.cache_data(ttl=60)
def get_monthly_salary_cost(selected_month_date: date):
    """
    Calculates the total salary cost for an entire month,
    respecting salary changes within that month.
    """
    total_monthly_salary = 0
    try:
        workers = db.table('workers').select('id').execute().data
        days_in_month = calendar.monthrange(selected_month_date.year, selected_month_date.month)[1]
        
        # --- FIX: Removed the faulty internal cache ---
        # Loop through each day of the month and get the *actual* salary cost for that day.
        for day in range(1, days_in_month + 1):
            current_date_for_loop = selected_month_date.replace(day=day)
            total_salary_for_this_day = 0
            
            for worker in workers:
                worker_id = worker['id']
                
                # Get the most recent salary valid for *this specific day*
                salary_entry = db.table('salary_history').select('daily_salary').eq('worker_id', worker_id).lte('start_date', current_date_for_loop.isoformat()).order('start_date', desc=True).limit(1).execute().data
                
                if salary_entry:
                    total_salary_for_this_day += salary_entry[0]['daily_salary']
            
            # Add this day's total salary to the month's total
            total_monthly_salary += total_salary_for_this_day
        
        # Return the final sum *after* the loop is finished
        return total_monthly_salary
            
    except Exception as e:
        st.error(f"خطأ في حساب الرواتب الشهرية: {e}")
        return 0


@st.cache_data(ttl=60)
def calculate_menu_item_cost(menu_item_id, effective_date: date):
    """
    Calculates the cost of goods for a single menu item from its recipe,
    respecting the stock item cost *as of* the effective_date.
    """
    try:
        recipe_response = db.table('menu_item_recipe').select(
            'quantity_used, stock_item_id'
        ).eq('menu_item_id', menu_item_id).execute()
        
        if not recipe_response.data:
            return 0
        
        total_cost = 0
        for ingredient in recipe_response.data:
            # Get the most recent cost for this stock item on or before the date
            cost_entry = db.table('stock_cost_history').select('cost_per_unit').eq('stock_item_id', ingredient['stock_item_id']).lte('start_date', effective_date.isoformat()).order('start_date', desc=True).limit(1).execute().data
            
            ingredient_cost = 0
            if cost_entry:
                ingredient_cost = cost_entry[0]['cost_per_unit']

            total_cost += ingredient['quantity_used'] * ingredient_cost
        return total_cost
    except Exception as e:
        st.error(f"خطأ في حساب تكلفة الصنف: {e}")
        return 0

def process_daily_sales(server_id, sales_dict: dict, sales_date: date):
    """
    Processes a server's entire daily sales report for a specific date.
    1. Creates one 'orders' entry for the server with the specified date.
    2. For each item in sales_dict, creates one 'order_items' entry with the total quantity.
    3. Decrements stock based on recipe * total quantity.
    """
    try:
        sales_timestamp = datetime.combine(sales_date, time(12, 0)).isoformat()
        order_response = db.table('orders').insert({
            'server_id': server_id,
            'timestamp': sales_timestamp 
        }).execute()
        
        if not order_response.data:
            st.error("فشل في إنشاء الطلب اليومي.")
            return 0
        
        order_id = order_response.data[0]['id']
        total_revenue = 0
        
        for item_id, details in sales_dict.items():
            quantity = details['quantity']
            if quantity == 0:
                continue
            
            # Get the price for the item *as of the sales date*
            price_entry = db.table('menu_price_history').select('sale_price').eq('menu_item_id', item_id).lte('start_date', sales_date.isoformat()).order('start_date', desc=True).limit(1).execute().data
            price_at_sale = price_entry[0]['sale_price'] if price_entry else 0
            
            # Calculate cost of goods for ONE item *as of the sales date*
            cost_at_sale_per_item = calculate_menu_item_cost(item_id, sales_date)
            
            db.table('order_items').insert({
                'order_id': order_id,
                'menu_item_id': item_id,
                'quantity': quantity,
                'price_at_sale': price_at_sale, 
                'cost_at_sale': cost_at_sale_per_item
            }).execute()
            
            total_revenue += price_at_sale * quantity
            
            recipe_response = db.table('menu_item_recipe').select(
                'stock_item_id, quantity_used'
            ).eq('menu_item_id', item_id).execute()
            
            for ingredient in recipe_response.data:
                total_amount_to_reduce = ingredient['quantity_used'] * quantity
                db.rpc('decrement_stock', {
                    'item_id': ingredient['stock_item_id'],
                    'amount_to_reduce': total_amount_to_reduce
                }).execute()
                
        return total_revenue
    except Exception as e:
        st.error(f"خطأ في معالجة المبيعات: {e}")
        return 0

# --- UI Rendering Functions (The "Frontend") ---

def render_monthly_dashboard():
    """Main dashboard showing current month's profit."""
    st.title("☕ لوحة التحكم الشهرية")
    st.header(f"تقرير الربح لشهر {date.today().strftime('%B %Y')}")

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
        
        # 2. Get Salaries (using new historical function)
        total_salaries = get_monthly_salary_cost(selected_month_date)
        
        # 3. Get Other Expenses for the month
        expense_data = db.table('monthly_expenses').select('amount').eq(
            'month', month_start_date.isoformat()
        ).execute().data
        total_expenses = sum(item['amount'] for item in expense_data)
        
        # 4. Calculate Net Profit
        total_costs_operating = total_salaries + total_expenses
        net_profit = gross_profit - total_costs_operating
        
        col1, col2, col3 = st.columns(3)
        col1.metric("إجمالي الإيرادات", f"{total_revenue:.3f} $")
        col2.metric("إجمالي الربح (الإيرادات - التكلفة)", f"{gross_profit:.3f} $")
        col3.metric("صافي الربح", f"{net_profit:.3f} $", delta_color=("inverse" if net_profit < 0 else "normal"))

        with st.expander("عرض تفاصيل الأرباح"):
            st.markdown(f"""
            - **إجمالي الإيرادات:** `{total_revenue:,.3f}`
            - **إجمالي تكلفة البضائع (COGS):** `({total_cogs:,.3f})`
            - **إجمالي الربح:** `{gross_profit:,.3f}`
            ---
            - **رواتب الموظفين:** `({total_salaries:,.3f})`
            - **المصروفات الأخرى:** `({total_expenses:,.3f})`
            - **إجمالي تكاليف التشغيل:** `({total_costs_operating:,.3f})`
            ---
            - **صافي الربح:** `{net_profit:,.3f}`
            """)
            
    except Exception as e:
        st.error(f"خطأ في إنشاء التقرير الشهري: {e}")

def render_daily_sales():
    """Page to enter the end-of-day sales for a server."""
    st.title("📝 تسجيل المبيعات اليومية")
    st.info("اختر نادلاً وتاريخاً، ثم أدخل الكمية الإجمالية لكل صنف تم بيعه.")
    
    try:
        servers = db.table('workers').select('id, name').eq('role', 'server').execute().data
        menu_items = db.table('menu_items').select('id, name').order('name').execute().data
        
        if not servers:
            st.warning("لم يتم العثور على نادلين. يرجى إضافة 'نادل' في صفحة 'الموظفون'.")
            return
        if not menu_items:
            st.warning("لم يتم العثور على أصناف في القائمة. يرجى إضافة أصناف في صفحة 'القائمة'.")
            return

        col1, col2 = st.columns(2)
        with col1:
            selected_server = st.selectbox("اختر النادل", servers, format_func=lambda x: x['name'])
        with col2:
            selected_date = st.date_input("اختر تاريخ المبيعات", date.today())
        
        with st.form("daily_sales_form"):
            st.header(f"مبيعات {selected_server['name']} في {selected_date.strftime('%Y-%m-%d')}")
            
            sales_dict = {}
            cols = st.columns(3)
            col_index = 0
            
            for item in menu_items:
                # Get the price for this item on the selected date to display
                price_entry = db.table('menu_price_history').select('sale_price').eq('menu_item_id', item['id']).lte('start_date', selected_date.isoformat()).order('start_date', desc=True).limit(1).execute().data
                current_price = price_entry[0]['sale_price'] if price_entry else 0
                
                with cols[col_index % 3]:
                    quantity = st.number_input(
                        f"كمية {item['name']} ({current_price}$)", 
                        min_value=0, 
                        step=1, 
                        key=f"qty_{item['id']}"
                    )
                    if quantity > 0:
                        sales_dict[item['id']] = {"quantity": quantity}
                col_index += 1

            submitted = st.form_submit_button("إرسال المبيعات اليومية", type="primary", use_container_width=True)
            if submitted:
                if selected_server and sales_dict and selected_date:
                    total_revenue = process_daily_sales(selected_server['id'], sales_dict, selected_date)
                    if total_revenue > 0:
                        st.success(f"تم تسجيل المبيعات بنجاح لـ {selected_server['name']} في {selected_date}. إجمالي الإيرادات: ${total_revenue:.3f}")
                        st.info("تم تحديث المخزون.")
                    else:
                        st.error("حدث خطأ أثناء معالجة المبيعات.")
                else:
                    st.warning("يرجى اختيار نادل، تاريخ، وإدخال كمية صنف واحد على الأقل.")
                
    except Exception as e:
        st.error(f"حدث خطأ: {e}")

def render_stock_management():
    """Page for viewing, adding, and restocking stock items."""
    st.title("📦 إدارة المخزون")
    
    tab1, tab2, tab3 = st.tabs(["عرض المخزون", "إضافة صنف جديد", "إعادة التخزين"])
    
    try:
        stock_data = db.table('stock_items').select('*').order('name').execute().data
    except Exception as e:
        st.error(f"فشل في تحميل المخزون: {e}")
        return

    with tab1:
        st.header("المخزون الحالي")
        st.info("قم بتوسيع أي صنف لرؤية التفاصيل، تغيير التكلفة، أو الحذف.")
        
        if not stock_data:
            st.warning("لم يتم العثور على أصناف في المخزون.")
        else:
            for item in stock_data:
                color = ""
                if (item['tracking_type'] in ['UNIT', 'MULTI-USE'] and item['current_quantity'] < 10) or \
                   (item['tracking_type'] == 'MANUAL' and item['current_quantity'] == 0):
                    color = "red"
                
                # Get current cost
                cost_entry = db.table('stock_cost_history').select('cost_per_unit').eq('stock_item_id', item['id']).order('start_date', desc=True).limit(1).execute().data
                current_cost = cost_entry[0]['cost_per_unit'] if cost_entry else 0

                label = f":{color}[{item['name']}] (الحالي: {item['current_quantity']} {item['unit_of_measure']}) | (التكلفة: ${current_cost:.3f})"
                
                with st.expander(label):
                    st.write(f"**نوع التتبع:** {item['tracking_type']}")
                    st.write(f"**معرّف الصنف:** `{item['id']}`")
                    
                    st.subheader("تغيير تكلفة الوحدة")
                    st.info("سيتم تطبيق التكلفة الجديدة على جميع المبيعات والهدر من اليوم فصاعداً.")
                    new_cost = st.number_input("التكلفة الجديدة للوحدة", min_value=0.0, format="%.3f", step=0.001, key=f"cost_{item['id']}", value=float(current_cost))
                    if st.button("تحديث التكلفة", key=f"upd_cost_{item['id']}"):
                        try:
                            db.table('stock_cost_history').insert({
                                'stock_item_id': item['id'],
                                'cost_per_unit': new_cost,
                                'start_date': date.today().isoformat()
                            }).execute()
                            st.success(f"تم تحديث تكلفة {item['name']} إلى ${new_cost:.3f} بدءاً من اليوم.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"خطأ في تحديث التكلفة: {e}")

                    st.divider()
                    if st.button("حذف هذا الصنف", key=f"del_stock_{item['id']}", type="primary"):
                        try:
                            recipe_links = db.table('menu_item_recipe').select('id').eq('stock_item_id', item['id']).execute().data
                            if recipe_links:
                                st.error(f"لا يمكن حذف '{item['name']}'. يتم استخدامه في {len(recipe_links)} وصفة. يرجى إزالته من جميع الوصفات أولاً.")
                            else:
                                db.table('stock_cost_history').delete().eq('stock_item_id', item['id']).execute() # Delete history
                                db.table('stock_items').delete().eq('id', item['id']).execute() # Delete item
                                st.success(f"تم حذف {item['name']}.")
                                st.rerun()
                        except Exception as e:
                            st.error(f"خطأ في حذف الصنف: {e}")

    with tab2:
        st.header("إضافة صنف جديد للمخزون")
        with st.form("new_stock_item_form"):
            name = st.text_input("اسم الصنف (مثل 'حبوب البن'، 'علبة كوكا كولا'، 'مناديل')")
            tracking_type = st.selectbox("نوع التتبع", options=['UNIT', 'MULTI-USE', 'MANUAL'], help="...")
            current_quantity = st.number_input("الكمية الأولية", min_value=0.0, step=0.001)
            unit_of_measure = st.text_input("وحدة القياس (مثل 'g'، 'ml'، 'pcs'، 'pack')")
            cost_per_unit = st.number_input("التكلفة الأولية للوحدة (تكلفتك)", min_value=0.0, format="%.3f", step=0.001)
            
            submitted = st.form_submit_button("إضافة الصنف")
            if submitted:
                if not name:
                    st.warning("يرجى ملء 'اسم الصنف'.")
                else:
                    try:
                        # 1. Insert the stock item
                        item_response = db.table('stock_items').insert({
                            'name': name,
                            'tracking_type': tracking_type,
                            'current_quantity': current_quantity,
                            'unit_of_measure': unit_of_measure
                        }).execute()
                        
                        new_item_id = item_response.data[0]['id']
                        
                        # 2. Insert its initial cost into the history table
                        db.table('stock_cost_history').insert({
                            'stock_item_id': new_item_id,
                            'cost_per_unit': cost_per_unit,
                            'start_date': date.today().isoformat()
                        }).execute()
                        
                        st.success(f"تمت إضافة {name} إلى المخزون!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"خطأ في إضافة الصنف: {e}")

    with tab3:
        st.header("إعادة التخزين")
        if not stock_data:
            st.warning("لا توجد أصناف لإعادة تخزينها.")
            return
        
        item_to_restock = st.selectbox(
            "اختر الصنف لإعادة التخزين",
            stock_data,
            format_func=lambda x: f"{x['name']} (الحالي: {x['current_quantity']} {x['unit_of_measure']})"
        )
        
        if item_to_restock:
            if item_to_restock['tracking_type'] in ['UNIT', 'MULTI-USE']:
                amount_to_add = st.number_input("الكمية المضافة", min_value=0.0, step=0.001)
                if st.button("إضافة إلى المخزون"):
                    new_quantity = item_to_restock['current_quantity'] + amount_to_add
                    try:
                        db.table('stock_items').update({'current_quantity': new_quantity}).eq('id', item_to_restock['id']).execute()
                        st.success(f"تمت إعادة تخزين {item_to_restock['name']}. الكمية الجديدة: {new_quantity}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"خطأ في إعادة التخزين: {e}")
            else: # MANUAL tracking
                if st.button(f"وضع علامة 'تمت إعادة التخزين' لـ '{item_to_restock['name']}'"):
                    try:
                        db.table('stock_items').update({'current_quantity': 1}).eq('id', item_to_restock['id']).execute()
                        st.success(f"تم وضع علامة 'تمت إعادة التخزين' لـ {item_to_restock['name']}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"خطأ في إعادة التخزين: {e}")

def render_menu_management():
    """Page for managing menu items and their recipes."""
    st.title("📋 إدارة القائمة والوصفات")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.header("إضافة صنف جديد للقائمة")
        with st.form("new_menu_item_form"):
            name = st.text_input("اسم صنف القائمة (مثل 'لاتيه')")
            sale_price = st.number_input("سعر البيع الأولي ($)", min_value=0.0, step=0.001, format="%.3f")
            submitted = st.form_submit_button("إضافة صنف للقائمة")
            
            if submitted and name and sale_price > 0:
                try:
                    # 1. Insert the menu item
                    item_response = db.table('menu_items').insert({'name': name}).execute()
                    new_item_id = item_response.data[0]['id']
                    
                    # 2. Insert its initial price into the history table
                    db.table('menu_price_history').insert({
                        'menu_item_id': new_item_id,
                        'sale_price': sale_price,
                        'start_date': date.today().isoformat()
                    }).execute()
                    
                    st.success(f"تمت إضافة {name} إلى القائمة.")
                    st.rerun()
                except Exception as e:
                    st.error(f"خطأ في إضافة الصنف: {e}")
    
    with col2:
        st.header("عرض وتعديل أسعار القائمة")
        try:
            menu_data = db.table('menu_items').select('*').order('name').execute().data
            if not menu_data:
                st.warning("لم يتم إضافة أصناف للقائمة بعد.")
            else:
                for item in menu_data:
                    # Get current price
                    price_entry = db.table('menu_price_history').select('sale_price').eq('menu_item_id', item['id']).order('start_date', desc=True).limit(1).execute().data
                    current_price = price_entry[0]['sale_price'] if price_entry else 0
                    
                    with st.expander(f"{item['name']} - (السعر الحالي: ${current_price})"):
                        st.subheader("تغيير سعر البيع")
                        st.info("سيتم تطبيق السعر الجديد على جميع المبيعات من اليوم فصاعداً.")
                        new_price = st.number_input("السعر الجديد ($)", min_value=0.0, step=0.001, format="%.3f", key=f"price_{item['id']}", value=float(current_price))
                        if st.button("تحديث السعر", key=f"upd_price_{item['id']}"):
                            try:
                                db.table('menu_price_history').insert({
                                    'menu_item_id': item['id'],
                                    'sale_price': new_price,
                                    'start_date': date.today().isoformat()
                                }).execute()
                                st.success(f"تم تحديث سعر {item['name']} إلى ${new_price} بدءاً من اليوم.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"خطأ في تحديث السعر: {e}")
                        
                        st.divider()
                        if st.button("حذف صنف القائمة هذا", key=f"del_menu_{item['id']}", type="primary"):
                            try:
                                db.table('menu_item_recipe').delete().eq('menu_item_id', item['id']).execute()
                                db.table('menu_price_history').delete().eq('menu_item_id', item['id']).execute()
                                db.table('menu_items').delete().eq('id', item['id']).execute()
                                st.success(f"تم حذف {item['name']}.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"خطأ في الحذف: {e}")
                            
        except Exception as e:
            st.error(f"فشل في تحميل القائمة: {e}")

    st.divider()
    
    st.header("تحديد وصفة القائمة")
    st.info("اربط ما تبيعه (مثل 'لاتيه') بما لديك في المخزون (مثل 'حبوب البن').")
    
    try:
        menu_data = db.table('menu_items').select('id, name').execute().data
        stock_data = db.table('stock_items').select('id, name, unit_of_measure').execute().data
        
        if not menu_data or not stock_data:
            st.warning("يرجى إضافة أصناف القائمة وأصناف المخزون أولاً.")
            return

        col1, col2, col3 = st.columns(3)
        with col1:
            menu_item = st.selectbox("اختر صنف القائمة", menu_data, format_func=lambda x: x['name'], key="recipe_menu_item")
        with col2:
            stock_item = st.selectbox("اختر مكون المخزون", stock_data, format_func=lambda x: f"{x['name']} ({x['unit_of_measure']})", key="recipe_stock_item")
        with col3:
            unit = next((item['unit_of_measure'] for item in stock_data if item['id'] == stock_item['id']), 'units')
            quantity_used = st.number_input(f"الكمية المستخدمة ({unit})", min_value=0.0, step=0.001, key="recipe_qty")
            
        if st.button("إضافة مكون إلى الوصفة", use_container_width=True):
            if menu_item and stock_item and quantity_used > 0:
                try:
                    db.table('menu_item_recipe').insert({
                        'menu_item_id': menu_item['id'],
                        'stock_item_id': stock_item['id'],
                        'quantity_used': quantity_used
                    }).execute()
                    st.success(f"تمت إضافة {quantity_used} {unit} من {stock_item['name']} إلى وصفة {menu_item['name']}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"خطأ في إضافة الوصفة: {e}")
                    
        if menu_item:
            recipe = db.table('menu_item_recipe').select(
                'id, quantity_used, stock_items(name, unit_of_measure)'
            ).eq('menu_item_id', menu_item['id']).execute().data
            
            if recipe:
                st.subheader(f"وصفة {menu_item['name']}")
                for r in recipe:
                    if r.get('stock_items'):
                        col1, col2 = st.columns([4,1])
                        col1.write(f"- {r['quantity_used']} {r['stock_items']['unit_of_measure']} من {r['stock_items']['name']}")
                        if col2.button("إزالة", key=f"del_recipe_{r['id']}", use_container_width=True):
                            db.table('menu_item_recipe').delete().eq('id', r['id']).execute()
                            st.rerun()
            
    except Exception as e:
        st.error(f"خطأ في تحميل بيانات الوصفة: {e}")


def render_staff_and_expenses():
    """Page for managing workers and monthly expenses."""
    st.title("👥 الموظفون و 🧾 المصروفات")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.header("إدارة الموظفين")
        with st.form("new_worker_form"):
            name = st.text_input("اسم الموظف")
            role = st.selectbox("الوظيفة", ["server", "barista"], format_func=lambda x: "نادل" if x == "server" else "باريستا")
            salary = st.number_input("الراتب اليومي الأولي ($)", min_value=0.0, step=0.001, format="%.3f")
            submitted = st.form_submit_button("إضافة موظف")
            
            if submitted and name and role and salary >= 0:
                try:
                    # 1. Insert worker
                    worker_response = db.table('workers').insert({
                        'name': name,
                        'role': role
                    }).execute()
                    new_worker_id = worker_response.data[0]['id']
                    
                    # 2. Insert initial salary
                    db.table('salary_history').insert({
                        'worker_id': new_worker_id,
                        'daily_salary': salary,
                        'start_date': date.today().isoformat()
                    }).execute()
                    
                    st.success(f"تمت إضافة {name} براتب يومي ${salary}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"خطأ في إضافة الموظف: {e}")
        
        st.subheader("الموظفون الحاليون")
        try:
            staff_data = db.table('workers').select('id, name, role').execute().data
            if not staff_data:
                st.warning("لم تتم إضافة موظفين بعد.")
            else:    
                for worker in staff_data:
                    role_ar = "نادل" if worker['role'] == "server" else "باريستا"
                    
                    # Get current salary
                    salary_entry = db.table('salary_history').select('daily_salary').eq('worker_id', worker['id']).order('start_date', desc=True).limit(1).execute().data
                    current_salary = salary_entry[0]['daily_salary'] if salary_entry else 0
                    
                    with st.expander(f"{worker['name']} ({role_ar}) - ${current_salary}/يوم"):
                        
                        st.subheader("تغيير الراتب")
                        st.info("سيتم تطبيق الراتب الجديد بدءاً من اليوم.")
                        new_salary = st.number_input("الراتب اليومي الجديد ($)", min_value=0.0, step=0.001, format="%.3f", key=f"salary_{worker['id']}", value=float(current_salary))
                        if st.button("تحديث الراتب", key=f"upd_salary_{worker['id']}"):
                            try:
                                db.table('salary_history').insert({
                                    'worker_id': worker['id'],
                                    'daily_salary': new_salary,
                                    'start_date': date.today().isoformat()
                                }).execute()
                                st.success(f"تم تحديث راتب {worker['name']} إلى ${new_salary} بدءاً من اليوم.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"خطأ في تحديث الراتب: {e}")
                        
                        st.divider()
                        if st.button("حذف الموظف", key=f"del_worker_{worker['id']}", type="primary"):
                            try:
                                orders = db.table('orders').select('id').eq('server_id', worker['id']).execute().data
                                if orders:
                                    st.error(f"لا يمكن حذف {worker['name']}. هو/هي مرتبط بـ {len(orders)} طلب.")
                                else:
                                    db.table('salary_history').delete().eq('worker_id', worker['id']).execute() # Delete salary history
                                    db.table('workers').delete().eq('id', worker['id']).execute() # Delete worker
                                    st.success(f"تم حذف {worker['name']}.")
                                    st.rerun()
                            except Exception as e:
                                st.error(f"خطأ في الحذف: {e}")
        except Exception as e:
            st.error(f"فشل في تحميل الموظفين: {e}")

    with col2:
        st.header("إدارة المصروفات الشهرية")
        with st.form("new_expense_form"):
            month = st.date_input("الشهر", date.today().replace(day=1))
            description = st.text_input("الوصف (مثل 'كهرباء'، 'إيجار')")
            amount = st.number_input("المبلغ ($)", min_value=0.0, step=0.001, format="%.3f")
            submitted = st.form_submit_button("إضافة مصروف")
            
            if submitted and month and description and amount > 0:
                try:
                    db.table('monthly_expenses').insert({
                        'month': month.isoformat(),
                        'description': description,
                        'amount': amount
                    }).execute()
                    st.success(f"تمت إضافة مصروف {description}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"خطأ في إضافة المصروف: {e}")

        st.subheader("المصروفات المسجلة")
        try:
            expense_data = db.table('monthly_expenses').select('id, month, description, amount').order('month', desc=True).execute().data
            if not expense_data:
                st.warning("لم يتم تسجيل مصروفات بعد.")
            else:
                for expense in expense_data:
                    with st.expander(f"{expense['month']} - {expense['description']} - ${expense['amount']}"):
                        if st.button("حذف المصروف", key=f"del_exp_{expense['id']}", type="primary"):
                            try:
                                db.table('monthly_expenses').delete().eq('id', expense['id']).execute()
                                st.success(f"تم حذف {expense['description']}.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"خطأ في الحذف: {e}")
        except Exception as e:
            st.error(f"فشل في تحميل المصروفات: {e}")

def render_reports():
    """Page for viewing profit reports."""
    st.title("📈 تقارير الأرباح")
    
    report_type = st.radio("اختر نوع التقرير", ["يومي", "شهري"], horizontal=True)
    
    if report_type == "يومي":
        st.subheader("تقرير الربح اليومي")
        selected_date = st.date_input("اختر التاريخ", date.today())
        
        start_day, end_day = get_today_range(selected_date)
        
        # 1. Get Revenue and COGS
        sales_data = db.table('order_items').select(
            'price_at_sale, cost_at_sale, quantity, orders!inner(timestamp)'
        ).gte('orders.timestamp', start_day).lte('orders.timestamp', end_day).execute().data
        
        total_revenue = sum(item['price_at_sale'] * item['quantity'] for item in sales_data)
        total_cogs = sum(item['cost_at_sale'] * item['quantity'] for item in sales_data)
        gross_profit = total_revenue - total_cogs
        
        # 2. Get Salaries for the day (using new historical function)
        total_salaries_today = get_daily_salary_cost(selected_date)
        
        # 3. Calculate Net Profit
        net_profit_today = gross_profit - total_salaries_today
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("إجمالي الإيرادات", f"{total_revenue:.3f} $")
        col2.metric("إجمالي الربح", f"{gross_profit:.3f} $")
        col3.metric("رواتب اليوم", f"({total_salaries_today:.3f}) $")
        col4.metric("صافي الربح اليومي", f"{net_profit_today:.3f} $", delta_color=("inverse" if net_profit_today < 0 else "normal"))

        with st.expander("عرض تفاصيل تقرير اليوم"):
            st.markdown(f"""
            - **إجمالي الإيرادات:** `{total_revenue:,.3f}`
            - **إجمالي تكلفة البضائع (COGS):** `({total_cogs:,.3f})`
            - **إجمالي الربح:** `{gross_profit:,.3f}`
            ---
            - **رواتب اليوم:** `({total_salaries_today:,.3f})`
            ---
            - **صافي الربح اليومي:** `{net_profit_today:,.3f}`
            """)

    if report_type == "شهري":
        st.subheader("تقرير الربح الشهري")
        selected_month_date = st.date_input("اختر الشهر", date.today())
        
        start_month_iso, end_month_iso, month_start_date = get_month_range(selected_month_date)
        
        try:
            # 1. Get Revenue and COGS for the month
            sales_data = db.table('order_items').select(
                'price_at_sale, cost_at_sale, quantity, orders!inner(timestamp)'
            ).gte('orders.timestamp', start_month_iso).lte('orders.timestamp', end_month_iso).execute().data
            
            total_revenue = sum(item['price_at_sale'] * item['quantity'] for item in sales_data)
            total_cogs = sum(item['cost_at_sale'] * item['quantity'] for item in sales_data)
            gross_profit = total_revenue - total_cogs
            
            # 2. Get Salaries (using new historical function)
            total_salaries = get_monthly_salary_cost(selected_month_date)
            
            # 3. Get Other Expenses for the month
            expense_data = db.table('monthly_expenses').select('amount').eq(
                'month', month_start_date.isoformat()
            ).execute().data
            total_expenses = sum(item['amount'] for item in expense_data)
            
            # 4. Calculate Net Profit
            total_costs_operating = total_salaries + total_expenses
            net_profit = gross_profit - total_costs_operating
            
            st.subheader(f"تقرير لشهر {selected_month_date.strftime('%B %Y')}")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("إجمالي الإيرادات", f"{total_revenue:.3f} $")
            col2.metric("إجمالي الربح (الإيرادات - التكلفة)", f"{gross_profit:.3f} $")
            col3.metric("صافي الربح", f"{net_profit:.3f} $", delta_color=("inverse" if net_profit < 0 else "normal"))

            with st.expander("عرض تفاصيل الأرباح"):
                st.markdown(f"""
                - **إجمالي الإيرادات:** `{total_revenue:,.3f}`
                - **إجمالي تكلفة البضائع (COGS):** `({total_cogs:,.3f})`
                - **إجمالي الربح:** `{gross_profit:,.3f}`
                ---
                - **رواتب الموظفين:** `({total_salaries:,.3f})`
                - **المصروفات الأخرى:** `({total_expenses:,.3f})`
                - **إجمالي تكاليف التشغيل:** `({total_costs_operating:,.3f})`
                ---
                - **صافي الربح:** `{net_profit:,.3f}`
                """)
                
        except Exception as e:
            st.error(f"خطأ في إنشاء التقرير الشهري: {e}")

def render_manage_orders():
    """Page to view and delete past daily sales orders."""
    st.title("🛒 إدارة الطلبات اليومية")
    st.info("هنا يمكنك مراجعة وحذف تقارير المبيعات اليومية بأكملها. حذف طلب سيزيله من جميع حسابات الأرباح. لن يتم إعادة تخزين الأصناف.")

    try:
        orders = db.table('orders').select(
            'id, timestamp, workers(name)'
        ).order('timestamp', desc=True).execute().data

        if not orders:
            st.warning("لم يتم العثور على طلبات.")
            return

        for order in orders:
            server_name = order['workers']['name'] if order.get('workers') else "نادل غير معروف"
            order_time = datetime.fromisoformat(order['timestamp']).strftime('%Y-%m-%d %I:%M %p')
            
            with st.expander(f"تقرير **{server_name}** من **{order_time}**"):
                
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
                                "الصنف": item_name,
                                "الكمية": item['quantity'],
                                "سعر الوحدة": f"${item['price_at_sale']:.3f}",
                                "إجمالي الإيرادات": f"${revenue:.3f}",
                                "إجمالي التكلفة": f"${cost:.3f}"
                            })
                            total_revenue += revenue
                            total_cost += cost
                    
                    st.dataframe(pd.DataFrame(item_data), hide_index=True, use_container_width=True)
                    st.markdown(f"**إجمالي الإيرادات:** `${total_revenue:.3f}` | **إجمالي التكلفة:** `${total_cost:.3f}`")

                else:
                    st.write("هذا الطلب لا يحتوي على أصناف.")

                if st.button("حذف هذا الطلب بالكامل", key=f"del_order_{order['id']}", type="primary"):
                    try:
                        db.table('orders').delete().eq('id', order['id']).execute()
                        st.success(f"تم حذف الطلب من {order_time}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"خطأ في حذف الطلب: {e}")
                        
    except Exception as e:
        st.error(f"خطأ في تحميل الطلبات: {e}")

def render_wastage():
    """Page to record menu items that were wasted (no revenue, stock decreased)."""
    st.title("🗑️ تسجيل الهدر")
    st.info("سجل الأصناف التي تم هدرها. سيؤدي هذا إلى تقليل المخزون وإضافة التكلفة إلى مصروفاتك الشهرية.")
    
    try:
        menu_items = db.table('menu_items').select('id, name').order('name').execute().data
            
        if not menu_items:
            st.warning("لم يتم العثور على أصناف في القائمة. يرجى إضافة أصناف في صفحة 'القائمة'.")
            return

        selected_date = st.date_input("اختر تاريخ الهدر", date.today())
        
        with st.form("wastage_form"):
            st.header(f"الهدر في {selected_date.strftime('%Y-%m-%d')}")
            
            wastage_dict = {}
            total_cost_of_wastage = 0
            
            cols = st.columns(3)
            col_index = 0
            
            for item in menu_items:
                with cols[col_index % 3]:
                    quantity = st.number_input(
                        f"كمية هدر {item['name']}", 
                        min_value=0, 
                        step=1, 
                        key=f"waste_qty_{item['id']}"
                    )
                    if quantity > 0:
                        # Calculate cost as of the selected wastage date
                        item_cost = calculate_menu_item_cost(item['id'], selected_date)
                        wastage_dict[item['id']] = {
                            "quantity": quantity,
                            "cost": item_cost
                        }
                        total_cost_of_wastage += item_cost * quantity
                col_index += 1

            submitted = st.form_submit_button("إرسال تقرير الهدر", type="primary", use_container_width=True)
            if submitted:
                if not wastage_dict:
                    st.warning("يرجى إدخال كمية صنف واحد على الأقل.")
                    return

                try:
                    for item_id, details in wastage_dict.items():
                        recipe_response = db.table('menu_item_recipe').select(
                            'stock_item_id, quantity_used'
                        ).eq('menu_item_id', item_id).execute()
                        
                        for ingredient in recipe_response.data:
                            total_amount_to_reduce = ingredient['quantity_used'] * details['quantity']
                            db.rpc('decrement_stock', {
                                'item_id': ingredient['stock_item_id'],
                                'amount_to_reduce': total_amount_to_reduce
                            }).execute()
                    
                    month_start = selected_date.replace(day=1)
                    db.table('monthly_expenses').insert({
                        'month': month_start.isoformat(),
                        'description': f"هدر في {selected_date.isoformat()}",
                        'amount': total_cost_of_wastage
                    }).execute()
                    
                    st.success(f"تم تسجيل الهدر بنجاح. التكلفة الإجمالية: ${total_cost_of_wastage:.3f}")
                    st.info("تم تحديث المخزون وإضافة التكلفة إلى المصروفات.")
                
                except Exception as e:
                    st.error(f"حدث خطأ أثناء معالجة الهدر: {e}")
                
    except Exception as e:
        st.error(f"حدث خطأ: {e}")


# --- Main Application Function ---
def main_app():
    """Renders the main application UI *after* successful login."""
    
    st.sidebar.title("مدير المقهى")
    
    if st.sidebar.button("تسجيل الخروج"):
        st.session_state.logged_in = False
        st.rerun()

    page = st.sidebar.radio(
        "التنقل",
        ["لوحة التحكم الشهرية", "تسجيل المبيعات اليومية", "إدارة الطلبات", "إدارة المخزون", "تسجيل الهدر", "إدارة القائمة", "الموظفون والمصروفات", "التقارير"]
    )

    if page == "لوحة التحكم الشهرية":
        render_monthly_dashboard()
    elif page == "تسجيل المبيعات اليومية":
        render_daily_sales()
    elif page == "إدارة الطلبات":
        render_manage_orders()
    elif page == "إدارة المخزون":
        render_stock_management()
    elif page == "تسجيل الهدر":
        render_wastage()
    elif page == "إدارة القائمة":
        render_menu_management()
    elif page == "الموظفون والمصروفات":
        render_staff_and_expenses()
    elif page == "التقارير":
        render_reports()

# --- Login Page Function ---
def show_login_page():
    """Renders the login form."""
    st.title("☕ تسجيل الدخول - مدير المقهى")
    
    try:
        app_username = st.secrets["APP_USERNAME"]
        app_password = st.secrets["APP_PASSWORD"]
    except KeyError:
        st.error("لم يتم العثور على بيانات اعتماد تسجيل الدخول في secrets.toml.")
        st.info("يرجى إضافة [APP_USERNAME] و [APP_PASSWORD] إلى ملف .streamlit/secrets.toml الخاص بك.")
        st.stop()

    with st.form("login_form"):
        username = st.text_input("اسم المستخدم")
        password = st.text_input("كلمة المرور", type="password")
        submitted = st.form_submit_button("تسجيل الدخول")

        if submitted:
            if username == app_username and password == app_password:
                st.session_state.logged_in = True
                st.rerun()
            else:
                st.error("اسم المستخدم أو كلمة المرور غير صحيحة")

# --- Main Control Flow ---
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

if st.session_state.logged_in:
    main_app()
else:
    show_login_page()

