import streamlit as st
import pandas as pd
import time
import random
from datetime import datetime
import pytz
from sp_api.api import CatalogItems, Products, ProductFees
from sp_api.base import Marketplaces

# ページ設定
st.set_page_config(page_title="Amazon SP-API Search Tool", layout="wide")

# --- 認証機能 ---
def check_password():
    """簡易ログイン機能"""
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if st.session_state.password_correct:
        return True

    st.markdown("## 🔐 ログイン")
    
    col1, col2 = st.columns([1, 2])
    with col1:
        user_id = st.text_input("ユーザーID", key="login_user")
        password = st.text_input("パスワード", type="password", key="login_pass")
        
        if st.button("ログイン"):
            # GitHubで編集して、あなただけのID/PASSに変更してください
            ADMIN_USER = "admin"
            ADMIN_PASS = "password123"
            
            if user_id == ADMIN_USER and password == ADMIN_PASS:
                st.session_state.password_correct = True
                st.rerun()
            else:
                st.error("IDまたはパスワードが間違っています")
    return False

# --- ユーティリティ関数 ---
def calculate_shipping_fee(height, length, width):
    """梱包サイズから送料を計算"""
    try:
        h, l, w = float(height), float(length), float(width)
        total_size = h + l + w
        if h <= 3 and total_size < 60: return 290
        elif total_size <= 60: return 580
        elif total_size <= 80: return 670
        elif total_size <= 100: return 780
        elif total_size <= 120: return 900
        elif total_size <= 140: return 1050
        elif total_size <= 160: return 1300
        elif total_size <= 170: return 2000
        elif total_size <= 180: return 2500
        elif total_size <= 200: return 3000
        else: return 'N/A'
    except:
        return 'N/A'

# --- SP-API ロジック ---
class AmazonSearcher:
    def __init__(self, credentials):
        self.credentials = credentials
        self.marketplace = Marketplaces.JP
        self.mp_id = 'A1VC38T7YXB528'
        self.logs = [] # デバッグログ用

    def log(self, message):
        """ログを記録"""
        ts = datetime.now().strftime('%H:%M:%S')
        self.logs.append(f"[{ts}] {message}")

    def get_prices_batch(self, asin_list):
        """【修正済】ASINリストを一括で価格取得する"""
        products_api = Products(credentials=self.credentials, marketplace=self.marketplace)
        price_map = {} 

        chunk_size = 20
        for i in range(0, len(asin_list), chunk_size):
            chunk = asin_list[i:i + chunk_size]
            self.log(f"Batch Requesting prices for {len(chunk)} items...")
            
            try:
                # ★修正: 第一引数(asin_list)として chunk をそのまま渡す
                res = products_api.get_competitive_pricing_for_asins(chunk, MarketplaceId=self.mp_id)
                
                if res and res.payload:
                    for item in res.payload:
                        asin = item.get('ASIN')
                        product = item.get('Product', {})
                        
                        best_price = float('inf')
                        best_seller = 'Unknown'
                        
                        # Competitive Pricing (カート価格)
                        comp = product.get('CompetitivePricing', {}).get('CompetitivePrices', [])
                        for cp in comp:
                            price_dict = cp.get('Price', {})
                            landed = (price_dict.get('LandedPrice') or {}).get('Amount')
                            listing = (price_dict.get('ListingPrice') or {}).get('Amount')
                            amount = landed or listing
                            
                            if amount and amount > 0:
                                if amount < best_price:
                                    best_price = amount
                                    best_seller = 'Cart Price'

                        # Lowest Offer (最安値)
                        lowest = product.get('LowestOfferListings', [])
                        for lo in lowest:
                            # 新品(New)のみ
                            if (lo.get('Qualifiers') or {}).get('ItemCondition') == 'New':
                                price_dict = lo.get('Price', {})
                                landed = (price_dict.get('LandedPrice') or {}).get('Amount')
                                listing = (price_dict.get('ListingPrice') or {}).get('Amount')
                                amount = landed or listing
                                
                                if amount and amount > 0:
                                    if amount < best_price:
                                        best_price = amount
                                        best_seller = 'Lowest Offer'

                        if best_price != float('inf'):
                            price_map[asin] = {
                                'price': best_price,
                                'seller': best_seller
                            }
                            self.log(f"Found batch price for {asin}: {best_price}")
                        else:
                            self.log(f"No batch price for {asin}")
                else:
                    self.log(f"Batch payload empty")
                
                time.sleep(1.0)
            except Exception as e:
                self.log(f"Batch Error: {str(e)}")
                pass
        
        return price_map

    def get_product_details(self, asin, pre_fetched_price_data=None):
        """詳細情報を取得（バッチデータ優先、なければ個別取得）"""
        try:
            catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
            
            res = None
            for _ in range(3):
                try:
                    res = catalog.get_catalog_item(
                        asin=asin,
                        marketplaceIds=[self.mp_id],
                        includedData=['attributes', 'salesRanks', 'summaries']
                    )
                    break
                except: time.sleep(1)
            
            info = {
                'asin': asin, 'jan': '', 'title': '', 'brand': '', 'category': '',
                'rank': 999999, 'rank_disp': '', 'price': 0, 'price_disp': '-',
                'points': '', 'fee_rate': '', 'seller': '', 'size': '', 'shipping': ''
            }
            
            list_price = 0

            if res and res.payload:
                data = res.payload
                if 'summaries' in data and data['summaries']:
                    info['title'] = data['summaries'][0].get('itemName', '')
                    info['brand'] = data['summaries'][0].get('brandName', '')

                if 'attributes' in data:
                    attrs = data['attributes']
                    if 'externally_assigned_product_identifier' in attrs:
                        for ext in attrs['externally_assigned_product_identifier']:
                            if ext.get('type') == 'ean':
                                info['jan'] = ext.get('value', '')
                                break
                    
                    if 'list_price' in attrs and attrs['list_price']:
                        for lp in attrs['list_price']:
                            if lp.get('currency') == 'JPY':
                                list_price = lp.get('value', 0)
                                break
                    
                    if 'item_package_dimensions' in attrs and attrs['item_package_dimensions']:
                        dim = attrs['item_package_dimensions'][0]
                        h = (dim.get('height') or {}).get('value', 0)
                        l = (dim.get('length') or {}).get('value', 0)
                        w = (dim.get('width') or {}).get('value', 0)
                        info['size'] = f"{h}x{l}x{w}"
                        s_fee = calculate_shipping_fee(h, l, w)
                        info['shipping'] = f"¥{s_fee}" if s_fee != 'N/A' else '-'

                if 'salesRanks' in data and data['salesRanks']:
                    ranks = data['salesRanks'][0].get('ranks', [])
                    if ranks:
                        r = ranks[0]
                        info['category'] = r.get('title', '')
                        info['rank'] = r.get('rank', 999999)
                        info['rank_disp'] = f"{info['rank']}位"

            # 2. 価格適用 (バッチデータ優先)
            if pre_fetched_price_data:
                info['price'] = pre_fetched_price_data['price']
                info['price_disp'] = f"¥{info['price']:,.0f}"
                info['seller'] = pre_fetched_price_data['seller']
            
            else:
                # バッチで取れなかった場合の個別取得 (最後の手段)
                products_api = Products(credentials=self.credentials, marketplace=self.marketplace)
                try:
                    # ★修正: item_condition (小文字) で指定
                    offers = products_api.get_item_offers(asin=asin, MarketplaceId=self.mp_id, item_condition='New')
                    
                    if offers and offers.payload and 'Offers' in offers.payload:
                        best_p = float('inf')
                        best_s = ''
                        best_pt = 0
                        for offer in offers.payload['Offers']:
                            p = (offer.get('ListingPrice') or {}).get('Amount', 0)
                            s = (offer.get('Shipping') or {}).get('Amount', 0)
                            total = p + s
                            if total > 0 and total < best_p:
                                best_p = total
                                best_s = offer.get('SellerId', '')
                                best_pt = (offer.get('Points') or {}).get('PointsNumber', 0)
                        
                        if best_p != float('inf'):
                            info['price'] = best_p
                            info['price_disp'] = f"¥{best_p:,.0f}"
                            info['seller'] = best_s
                            if best_pt > 0: info['points'] = f"{(best_pt/best_p)*100:.1f}%"
                except Exception as e:
                    # self.log(f"Single fetch error {asin}: {e}") 
                    pass

            # 3. 参考価格フォールバック
            if info['price'] == 0 and list_price > 0:
                info['price_disp'] = f"¥{list_price:,.0f} (参考)"
                info['seller'] = 'Ref Only'

            # 4. 手数料
            if info['price'] > 0:
                try:
                    fees_api = ProductFees(credentials=self.credentials, marketplace=self.marketplace)
                    f_res = fees_api.get_product_fees_estimate_for_asin(
                        asin=asin, price=info['price'], is_fba=True, 
                        identifier=f'fee-{asin}', currency='JPY', marketplace_id=self.mp_id
                    )
                    if f_res and f_res.payload:
                        fees = f_res.payload.get('FeesEstimateResult', {}).get('FeesEstimate', {}).get('FeeDetailList', [])
                        for fee in fees:
                            if fee.get('FeeType') == 'ReferralFee':
                                amt = (fee.get('FinalFee') or {}).get('Amount', 0)
                                if amt > 0:
                                    info['fee_rate'] = f"{(amt/info['price'])*100:.1f}%"
                except: pass

            return info

        except Exception as e:
            self.log(f"Details error {asin}: {e}")
            return None

    def search_by_keywords(self, keywords, max_results):
        """キーワード検索"""
        catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
        found_items = []
        page_token = None
        status_text = st.empty()
        
        scan_limit = int(max_results * 1.5)
        if scan_limit < 20: scan_limit = 20

        while len(found_items) < scan_limit:
            params = {
                'keywords': [keywords], 'marketplaceIds': [self.mp_id],
                'includedData': ['salesRanks'], 'pageSize': 20
            }
            if page_token: params['pageToken'] = page_token

            try:
                res = None
                for _ in range(3):
                    try:
                        res = catalog.search_catalog_items(**params)
                        break
                    except: time.sleep(1)
                
                if res and res.payload:
                    items = res.payload.get('items', [])
                    if not items: break
                    for item in items:
                        asin = item.get('asin')
                        rank_val = 9999999 
                        if 'salesRanks' in item and item['salesRanks']:
                            ranks_list = item['salesRanks'][0].get('ranks', [])
                            if ranks_list: rank_val = ranks_list[0].get('rank', 9999999)
                        found_items.append({'asin': asin, 'rank': rank_val})
                    status_text.text(f"候補を検索中... {len(found_items)}件 取得")
                    page_token = res.next_token
                    if not page_token: break
                else: break
                time.sleep(1)
            except: break
        
        sorted_items = sorted(found_items, key=lambda x: x['rank'])
        return [item['asin'] for item in sorted_items][:max_results]

    def search_by_jan(self, jan_code):
        """JAN検索"""
        catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
        try:
            res = catalog.search_catalog_items(keywords=[jan_code], marketplaceIds=[self.mp_id])
            if res and res.payload and 'items' in res.payload:
                items = res.payload['items']
                if items: return items[0].get('asin')
        except: pass
        return None

# --- メインアプリ ---
def main():
    if not check_password(): return

    st.title("📦 Amazon SP-API 商品リサーチツール")

    with st.sidebar:
        st.header("⚙️ 設定")
        if "LWA_APP_ID" in st.secrets:
            st.success("✅ 認証設定済み")
            lwa_app_id = st.secrets["LWA_APP_ID"]
            lwa_client_secret = st.secrets["LWA_CLIENT_SECRET"]
            refresh_token = st.secrets["REFRESH_TOKEN"]
            aws_access_key = st.secrets["AWS_ACCESS_KEY"]
            aws_secret_key = st.secrets["AWS_SECRET_KEY"]
        else:
            st.warning("Secrets未設定")
            lwa_app_id = st.text_input("LWA App ID", type="password")
            lwa_client_secret = st.text_input("LWA Client Secret", type="password")
            refresh_token = st.text_input("Refresh Token", type="password")
            aws_access_key = st.text_input("AWS Access Key", type="password")
            aws_secret_key = st.text_input("AWS Secret Key", type="password")

    st.markdown("### 🔍 検索条件")
    col_mode, col_limit = st.columns([2, 1])
    with col_mode:
        search_mode = st.selectbox("検索モード", ["JANコードリスト", "ASINリスト", "ブランド検索", "カテゴリ/キーワード検索"])
    with col_limit:
        max_results = st.slider("取得件数上限", 10, 200, 50, 10)

    input_data = ""
    if search_mode in ["JANコードリスト", "ASINリスト"]:
        input_data = st.text_area(f"{search_mode} (1行に1つ)", height=150)
    else:
        input_data = st.text_input(f"{search_mode} キーワード")

    if st.button("検索開始", type="primary"):
        if not (lwa_app_id and lwa_client_secret and refresh_token):
            st.error("API設定が必要です")
            return

        credentials = {
            'refresh_token': refresh_token, 'lwa_app_id': lwa_app_id,
            'lwa_client_secret': lwa_client_secret,
            'aws_access_key': aws_access_key, 'aws_secret_key': aws_secret_key,
            'role_arn': st.secrets.get("ROLE_ARN", "")
        }

        searcher = AmazonSearcher(credentials)
        target_asins = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        status_text.info("リスト作成中...")
        if search_mode == "JANコードリスト":
            jan_list = [line.strip() for line in input_data.split('\n') if line.strip()]
            for i, jan in enumerate(jan_list):
                status_text.text(f"JAN変換: {jan}")
                asin = searcher.search_by_jan(jan)
                if asin: target_asins.append(asin)
                time.sleep(0.5)
                progress_bar.progress((i+1)/len(jan_list)*0.3)
        elif search_mode == "ASINリスト":
            target_asins = [line.strip() for line in input_data.split('\n') if line.strip()]
            progress_bar.progress(30)
        else:
            target_asins = searcher.search_by_keywords(input_data, max_results)
            progress_bar.progress(30)

        if not target_asins:
            st.error("商品が見つかりません")
            return

        st.success(f"{len(target_asins)}件のASINを特定。価格を一括取得します...")
        price_map = searcher.get_prices_batch(target_asins)
        
        results = []
        df_placeholder = st.empty()
        
        for i, asin in enumerate(target_asins):
            status_text.text(f"詳細取得中: {asin} ({i+1}/{len(target_asins)})")
            
            pre_price = price_map.get(asin)
            detail = searcher.get_product_details(asin, pre_fetched_price_data=pre_price)
            
            if detail: results.append(detail)
            
            if results:
                df = pd.DataFrame(results)
                disp = {
                    'title':'商品名', 'brand':'ブランド', 'price_disp':'価格', 
                    'rank_disp':'ランキング', 'category':'カテゴリ',
                    'points':'ポイント率', 'fee_rate':'手数料率', 'asin':'ASIN'
                }
                cols = [c for c in disp.keys() if c in df.columns]
                df_placeholder.dataframe(df[cols].rename(columns=disp), use_container_width=True)

            progress_bar.progress(min(0.3 + ((i+1)/len(target_asins)*0.7), 1.0))
            time.sleep(0.2)

        status_text.success("完了！")
        progress_bar.progress(100)

        with st.expander("デバッグログを表示 (API通信状況)"):
            for log in searcher.logs:
                st.text(log)

        if results:
            df_final = pd.DataFrame(results)
            df_final = df_final.drop(columns=['rank', 'price'], errors='ignore')
            jst = pytz.timezone('Asia/Tokyo')
            fname = f"amazon_research_{datetime.now(jst).strftime('%Y%m%d_%H%M%S')}.csv"
            st.download_button("📥 CSVダウンロード", df_final.to_csv(index=False).encode('utf-8_sig'), fname, "text/csv", type="primary")

if __name__ == "__main__":
    main()
