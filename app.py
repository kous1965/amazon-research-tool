import streamlit as st
import pandas as pd
import time
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

    def get_prices_batch(self, asin_list):
        """【新機能】ASINリストを受け取り、一括で価格情報を取得する（高速・安定）"""
        products_api = Products(credentials=self.credentials, marketplace=self.marketplace)
        price_map = {} # {asin: {'price': 1000, 'points': 10, 'seller': 'Amazon'}}

        # 20件ずつ分割してリクエスト（API制限対策）
        chunk_size = 20
        for i in range(0, len(asin_list), chunk_size):
            chunk = asin_list[i:i + chunk_size]
            try:
                # get_pricing は最大20件まで同時に取得可能
                res = products_api.get_pricing(MarketplaceId=self.mp_id, Asins=chunk, ItemType='Asin')
                
                if res and res.payload:
                    for item in res.payload:
                        asin = item.get('ASIN')
                        product = item.get('Product', {})
                        
                        best_price = float('inf')
                        best_seller = 'Unknown'
                        
                        # 1. Competitive Pricing (カート価格)
                        comp = product.get('CompetitivePricing', {}).get('CompetitivePrices', [])
                        for cp in comp:
                            price_dict = cp.get('Price', {})
                            # 安全な取り出し (or {} を追加してクラッシュ防止)
                            landed = (price_dict.get('LandedPrice') or {}).get('Amount')
                            listing = (price_dict.get('ListingPrice') or {}).get('Amount')
                            amount = landed or listing
                            
                            if amount and amount > 0:
                                if amount < best_price:
                                    best_price = amount
                                    best_seller = 'Cart Price' # カート価格

                        # 2. Lowest Offer (最安値)
                        lowest = product.get('LowestOfferListings', [])
                        for lo in lowest:
                            # 新品(New)のみ対象
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
                                'seller': best_seller,
                                'points': 0 # pricing APIではポイントが取れないことが多い
                            }
                
                time.sleep(0.5) # バッチ間の待機
            except Exception as e:
                print(f"Batch price fetch error: {e}")
                pass
        
        return price_map

    def get_product_details(self, asin, pre_fetched_price_data=None):
        """詳細情報を取得（バッチ取得した価格データがあればそれを使う）"""
        try:
            # 1. Catalog API (基本情報)
            catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
            res = catalog.get_catalog_item(
                asin=asin,
                marketplaceIds=[self.mp_id],
                includedData=['attributes', 'salesRanks', 'summaries']
            )
            
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
                    
                    # 参考価格の取得
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

            # 2. 価格の適用 (バッチデータ優先)
            if pre_fetched_price_data:
                # バッチですでに価格が取れている場合
                info['price'] = pre_fetched_price_data['price']
                info['price_disp'] = f"¥{info['price']:,.0f}"
                info['seller'] = pre_fetched_price_data['seller']
            
            else:
                # バッチで取れなかった場合のみ、個別にAPIを叩く (バックアップ)
                try:
                    products_api = Products(credentials=self.credentials, marketplace=self.marketplace)
                    # 全コンディション取得 (item_condition指定なし)
                    offers = products_api.get_item_offers(asin=asin, MarketplaceId=self.mp_id)
                    
                    if offers and offers.payload and 'Offers' in offers.payload:
                        best_p = float('inf')
                        best_s = ''
                        best_pt = 0
                        
                        for offer in offers.payload['Offers']:
                            # クラッシュ防止: or {} を追加
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
                            if best_pt > 0:
                                info['points'] = f"{(best_pt/best_p)*100:.1f}%"
                except:
                    pass

            # 3. 価格がどうしても取れなかった場合の参考価格表示
            if info['price'] == 0 and list_price > 0:
                info['price_disp'] = f"¥{list_price:,.0f} (参考)"
                info['seller'] = 'Ref Only'

            # 4. 手数料計算
            if info['price'] > 0:
                try:
                    fees_api = ProductFees(credentials=self.credentials, marketplace=self.marketplace)
                    f_res = fees_api.get_product_fees_estimate_for_asin(
                        asin=asin, price=info['price'], is_fba=True, 
                        identifier=f'fee-{asin}', currency='JPY', marketplace_id=
