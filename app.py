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
        
        # 送料計算ルール（必要に応じて金額を修正してください）
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

    def get_product_details(self, asin):
        """ASINから詳細情報を取得（修正版：エラー回避＆価格取得強化）"""
        try:
            # 1. Catalog API (基本情報)
            # ★修正: 'offers' を削除しました（これがエラーの原因でした）
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

            if res and res.payload:
                data = res.payload
                
                # 基本情報
                if 'summaries' in data and data['summaries']:
                    info['title'] = data['summaries'][0].get('itemName', '')
                    info['brand'] = data['summaries'][0].get('brandName', '')

                # JANコード
                if 'attributes' in data:
                    attrs = data['attributes']
                    if 'externally_assigned_product_identifier' in attrs:
                        for ext in attrs['externally_assigned_product_identifier']:
                            if ext.get('type') == 'ean':
                                info['jan'] = ext.get('value', '')
                                break
                    
                    # サイズ計算
                    if 'item_package_dimensions' in attrs and attrs['item_package_dimensions']:
                        dim = attrs['item_package_dimensions'][0]
                        h = dim.get('height', {}).get('value', 0)
                        l = dim.get('length', {}).get('value', 0)
                        w = dim.get('width', {}).get('value', 0)
                        info['size'] = f"{h}x{l}x{w}"
                        s_fee = calculate_shipping_fee(h, l, w)
                        info['shipping'] = f"¥{s_fee}" if s_fee != 'N/A' else '-'

                # ランキング
                if 'salesRanks' in data and data['salesRanks']:
                    ranks = data['salesRanks'][0].get('ranks', [])
                    if ranks:
                        r = ranks[0]  # 大分類
                        info['category'] = r.get('title', '')
                        info['rank'] = r.get('rank', 999999)
                        info['rank_disp'] = f"{info['rank']}位"

            # 2. 価格取得フェーズ (Plan A -> Plan B)
            products_api = Products(credentials=self.credentials, marketplace=self.marketplace)
            
            # --- Plan A: get_item_offers (詳細な出品者情報から取得) ---
            try:
                offers = products_api.get_item_offers(asin=asin, MarketplaceId=self.mp_id, item_condition='New')
                
                if offers and offers.payload and 'Offers' in offers.payload:
                    found_buybox = False
                    lowest_price = float('inf')
                    best_offer = None

                    for offer in offers.payload['Offers']:
                        listing_price = offer.get('ListingPrice', {}).get('Amount', 0)
                        shipping = offer.get('Shipping', {}).get('Amount', 0)
                        total_price = listing_price + shipping
                        
                        if total_price == 0: continue

                        # カート獲得者を優先
                        if offer.get('IsBuyBoxWinner', False):
                            best_offer = offer
                            info['price'] = total_price
                            found_buybox = True
                            break 
                        
                        # 最安値を記録
                        if total_price < lowest_price:
                            lowest_price = total_price
                            if not found_buybox:
                                best_offer = offer
                                info['price'] = total_price

                    if best_offer:
                        p = info['price']
                        info['price_disp'] = f"¥{p:,.0f}"
                        info['seller'] = best_offer.get('SellerId', '')
                        points = best_offer.get('Points', {}).get('PointsNumber', 0)
                        if points > 0 and p > 0:
                            info['points'] = f"{(points/p)*100:.1f}%"
            except Exception:
                pass

            # --- Plan B: get_pricing (Plan A失敗時の強力なバックアップ) ---
            # カートボックス価格(Competitive Price)を取得しに行きます。
            # セール価格などはここに反映されていることが多いです。
            if info['price'] == 0:
                try:
                    price_res = products_api.get_pricing(MarketplaceId=self.mp_id, Asins=[asin], ItemType='Asin')
                    if price_res and price_res.payload:
                        product_data = price_res.payload[0].get('Product', {})
                        
                        # 優先順位1: Competitive Price (カート価格相当)
                        comp_prices = product_data.get('CompetitivePricing', {}).get('CompetitivePrices', [])
                        if comp_prices:
                            price_obj = comp_prices[0].get('Price', {})
                            # 送料込み(LandedPrice)があれば優先、なければ本体価格(ListingPrice)
                            amount = price_obj.get('LandedPrice', {}).get('Amount') or price_obj.get('ListingPrice', {}).get('Amount', 0)
                            
                            if amount > 0:
                                info['price'] = amount
                                info['price_disp'] = f"¥{amount:,.0f}"
                                info['seller'] = 'Amazon/Others'
                        
                        # 優先順位2: 最安値情報 (Lowest Offer)
                        if info['price'] == 0:
                             lowest_offers = product_data.get('LowestOfferListings', [])
                             if lowest_offers:
                                 price_obj = lowest_offers[0].get('Price', {})
                                 amount = price_obj.get('LandedPrice', {}).get('Amount') or price_obj.get('ListingPrice', {}).get('Amount', 0)
                                 if amount > 0:
                                    info['price'] = amount
                                    info['price_disp'] = f"¥{amount:,.0f}"
                                    info['seller'] = 'Lowest Offer'
                except Exception:
                    pass

            # 3. 手数料 (Fees API)
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
                                amt = fee.get('FinalFee', {}).get('Amount', 0)
                                if amt > 0:
                                    info['fee_rate'] = f"{(amt/info['price'])*100:.1f}%"
                except Exception:
                    pass

            return info

        except Exception as e:
            # 致命的なエラーでも止まらないようにNoneを返す
            # st.error(f"商品詳細取得エラー ({asin}): {e}") # エラー表示を抑制する場合
            print(f"Error fetching {asin}: {e}")
            return None

    def search_by_keywords(self, keywords, max_results):
        """キーワード検索後、ランキング順（昇順）にソートしてASINを取得"""
        catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
        
        found_items = []
        page_token = None
        status_text = st.empty()
        
        # 1.5倍スキャン
        scan_limit = int(max_results * 1.5)
        if scan_limit < 20: scan_limit = 20

        while len(found_items) < scan_limit:
            params = {
                'keywords': [keywords],
                'marketplaceIds': [self.mp_id],
                'includedData': ['salesRanks'],
                'pageSize': 20
            }
            if page_token:
                params['pageToken'] = page_token

            try:
                res = catalog.search_catalog_items(**params)
                if res and res.payload:
                    items = res.payload.get('items', [])
                    if not items: break
                    
                    for item in items:
                        asin = item.get('asin')
                        rank_val = 9999999 
                        if 'salesRanks' in item and item['salesRanks']:
                            ranks_list = item['salesRanks'][0].get('ranks', [])
                            if ranks_list:
                                rank_val = ranks_list[0].get('rank', 9999999)
                        found_items.append({'asin': asin, 'rank': rank_val})
                    
                    status_text.text(f"候補を検索中... {len(found_items)}件 取得")
                    page_token = res.next_token
                    if not page_token: break
                else:
                    break
                time.sleep(1)
            except Exception as e:
                st.error(f"検索エラー: {e}")
                break
        
        # ソートと抽出
        sorted_items = sorted(found_items, key=lambda x: x['rank'])
        final_asins = [item['asin'] for item in sorted_items][:max_results]
        return final_asins

    def search_by_jan(self, jan_code):
        """JANコードからASINを取得"""
        catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
        try:
            res = catalog.search_catalog_items(keywords=[jan_code], marketplaceIds=[self.mp_id])
            if res and res.payload and 'items' in res.payload:
                items = res.payload['items']
                if items:
                    return items[0].get('asin')
        except:
            pass
        return None

# --- メインアプリ ---
def main():
    if not check_password():
        return

    st.title("📦 Amazon SP-API 商品リサーチツール")

    # サイドバー
    with st.sidebar:
        st.header("⚙️ 設定")
        if "LWA_APP_ID" in st.secrets:
            st.success("✅ 認証情報は設定済みです")
            st.info("キーは安全に保護されています。")
            lwa_app_id = st.secrets["LWA_APP_ID"]
            lwa_client_secret = st.secrets["LWA_CLIENT_SECRET"]
            refresh_token = st.secrets["REFRESH_TOKEN"]
            aws_access_key = st.secrets["AWS_ACCESS_KEY"]
            aws_secret_key = st.secrets["AWS_SECRET_KEY"]
        else:
            st.warning("Secretsが設定されていません。手動入力してください。")
            lwa_app_id = st.text_input("LWA App ID", type="password")
            lwa_client_secret = st.text_input("LWA Client Secret", type="password")
            refresh_token = st.text_input("Refresh Token", type="password")
            aws_access_key = st.text_input("AWS Access Key", type="password")
            aws_secret_key = st.text_input("AWS Secret Key", type="password")

    # 検索条件
    st.markdown("### 🔍 検索条件")
    col_mode, col_limit = st.columns([2, 1])
    with col_mode:
        search_mode = st.selectbox(
            "検索モードを選択",
            ["JANコードリスト", "ASINリスト", "ブランド検索", "カテゴリ/キーワード検索"]
        )
    with col_limit:
        max_results = st.slider("取得件数上限", 10, 200, 50, 10)

    input_data = ""
    if search_mode in ["JANコードリスト", "ASINリスト"]:
        input_data = st.text_area(f"{search_mode}を入力 (1行に1つ)", height=150)
    else:
        input_data = st.text_input(f"{search_mode} キーワードを入力")

    if st.button("検索開始", type="primary"):
        if not (lwa_app_id and lwa_client_secret and refresh_token):
            st.error("API認証情報を設定してください。")
            return
        if not input_data:
            st.warning("検索条件を入力してください。")
            return

        credentials = {
            'refresh_token': refresh_token,
            'lwa_app_id': lwa_app_id,
            'lwa_client_secret': lwa_client_secret,
            'aws_access_key': aws_access_key,
            'aws_secret_key': aws_secret_key,
            'role_arn': st.secrets.get("ROLE_ARN", "")
        }

        searcher = AmazonSearcher(credentials)
        target_asins = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        # 1. ASINリスト生成
        status_text.info("ASINリストを作成中...")
        if search_mode == "JANコードリスト":
            jan_list = [line.strip() for line in input_data.split('\n') if line.strip()]
            for i, jan in enumerate(jan_list):
                status_text.text(f"JAN変換中: {jan} ({i+1}/{len(jan_list)})")
                asin = searcher.search_by_jan(jan)
                if asin: target_asins.append(asin)
                time.sleep(0.5)
                progress_bar.progress((i + 1) / len(jan_list) * 0.3)
        elif search_mode == "ASINリスト":
            target_asins = [line.strip() for line in input_data.split('\n') if line.strip()]
            progress_bar.progress(30)
        else:
            target_asins = searcher.search_by_keywords(input_data, max_results)
            progress_bar.progress(30)

        if not target_asins:
            st.error("対象の商品が見つかりませんでした。")
            return

        st.success(f"{len(target_asins)} 件の商品ASINを特定。詳細情報を取得します...")
        
        # 2. 詳細情報取得
        results = []
        df_placeholder = st.empty()
        
        for i, asin in enumerate(target_asins):
            status_text.text(f"データ取得中: {asin} ({i+1}/{len(target_asins)})")
            time.sleep(1.5) 
            
            detail = searcher.get_product_details(asin)
            if detail:
                results.append(detail)
            
            if results:
                df_current = pd.DataFrame(results)
                display_cols = {
                    'title': '商品名', 'brand': 'ブランド', 'price_disp': '価格', 
                    'rank_disp': 'ランキング', 'category': 'カテゴリ',
                    'points': 'ポイント率', 'fee_rate': '手数料率', 'asin': 'ASIN'
                }
                cols_to_show = [c for c in display_cols.keys() if c in df_current.columns]
                df_show = df_current[cols_to_show].rename(columns=display_cols)
                df_placeholder.dataframe(df_show, use_container_width=True)

            current_progress = 0.3 + ((i + 1) / len(target_asins) * 0.7)
            progress_bar.progress(min(current_progress, 1.0))

        status_text.success("完了！")
        progress_bar.progress(100)

        # 3. ダウンロード
        if results:
            df_final = pd.DataFrame(results)
            df_final = df_final.drop(columns=['rank', 'price'], errors='ignore')
            
            jst = pytz.timezone('Asia/Tokyo')
            filename = f"amazon_research_{datetime.now(jst).strftime('%Y%m%d_%H%M%S')}.csv"
            csv = df_final.to_csv(index=False).encode('utf-8_sig')
            
            st.download_button(
                label="📥 CSVをダウンロード",
                data=csv,
                file_name=filename,
                mime='text/csv',
                type="primary"
            )

if __name__ == "__main__":
    main()
