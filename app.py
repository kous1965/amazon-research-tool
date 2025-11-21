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
            # ここでIDとパスワードを設定（本番運用時は環境変数などで管理推奨）
            ADMIN_USER = "Okadaya"
            ADMIN_PASS = "Akio6583a"  # 任意のパスワードに変更してください
            
            if user_id == ADMIN_USER and password == ADMIN_PASS:
                st.session_state.password_correct = True
                st.rerun()
            else:
                st.error("IDまたはパスワードが間違っています")
    return False

# --- ユーティリティ関数 ---
def calculate_shipping_fee(height, length, width):
    """梱包サイズから送料を計算 (旧sp_api_app.pyより移植)"""
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

    def get_product_details(self, asin):
        """ASINから詳細情報を取得"""
        try:
            # Catalog API
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
                        r = ranks[0]   # ← 「最初（大分類）」に変更
                        info['category'] = r.get('title', '')
                        info['rank'] = r.get('rank', 999999)
                        info['rank_disp'] = f"{info['rank']}位"

            # 価格・カート情報 (Products API)
            try:
                products_api = Products(credentials=self.credentials, marketplace=self.marketplace)
                offers = products_api.get_item_offers(asin=asin, MarketplaceId=self.mp_id, item_condition='New')
                
                if offers and offers.payload and 'Offers' in offers.payload:
                    for offer in offers.payload['Offers']:
                        if offer.get('IsBuyBoxWinner', False):
                            price = offer.get('ListingPrice', {}).get('Amount', 0)
                            shipping = offer.get('Shipping', {}).get('Amount', 0)
                            points = offer.get('Points', {}).get('PointsNumber', 0)
                            
                            total_price = price + shipping
                            info['price'] = total_price
                            info['price_disp'] = f"¥{total_price:,.0f}"
                            info['seller'] = offer.get('SellerId', '')
                            
                            if points > 0 and total_price > 0:
                                info['points'] = f"{(points/total_price)*100:.1f}%"
                            break
            except Exception:
                pass # 価格取得エラーは無視

            # 手数料 (Fees API)
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
            st.error(f"商品詳細取得エラー ({asin}): {e}")
            return None

    def search_by_keywords(self, keywords, max_results):
        """キーワード（ブランド/カテゴリ/任意）で検索してASINリストを取得"""
        catalog = CatalogItems(credentials=self.credentials, marketplace=self.marketplace)
        all_asins = []
        page_token = None
        
        status_text = st.empty()
        
        while len(all_asins) < max_results:
            params = {
                'keywords': [keywords],
                'marketplaceIds': [self.mp_id],
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
                        if len(all_asins) >= max_results: break
                        all_asins.append(item.get('asin'))
                    
                    status_text.text(f"検索中... {len(all_asins)}件 ヒット")
                    
                    page_token = res.next_token
                    if not page_token: break
                else:
                    break
                time.sleep(1) # API制限対策
            except Exception as e:
                st.error(f"検索エラー: {e}")
                break
                
        return all_asins[:max_results]

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

    st.title("📦 Amazon SP-API 商品リサーチツール(made by 岡田屋)")

# サイドバー：API設定
    with st.sidebar:
        st.header("⚙️ 設定")
        
        # Secretsに設定があるか確認
        if "LWA_APP_ID" in st.secrets:
            st.success("✅ 認証情報はクラウド設定から読み込まれました")
            st.info("キーは安全に保護されています。")
            
            # 変数に直接代入（画面には表示しない）
            lwa_app_id = st.secrets["LWA_APP_ID"]
            lwa_client_secret = st.secrets["LWA_CLIENT_SECRET"]
            refresh_token = st.secrets["REFRESH_TOKEN"]
            aws_access_key = st.secrets["AWS_ACCESS_KEY"]
            aws_secret_key = st.secrets["AWS_SECRET_KEY"]
        else:
            # Secretsがない場合のみ入力欄を表示（テスト用など）
            st.warning("Secretsが設定されていません。手動入力してください。")
            lwa_app_id = st.text_input("LWA App ID", type="password")
            lwa_client_secret = st.text_input("LWA Client Secret", type="password")
            refresh_token = st.text_input("Refresh Token", type="password")
            aws_access_key = st.text_input("AWS Access Key", type="password")
            aws_secret_key = st.text_input("AWS Secret Key", type="password")

    # 検索条件の設定
    st.markdown("### 🔍 検索条件")
    col_mode, col_limit = st.columns([2, 1])
    
    with col_mode:
        search_mode = st.selectbox(
            "検索モードを選択",
            ["JANコードリスト", "ASINリスト", "ブランド検索", "カテゴリ/キーワード検索"]
        )

    with col_limit:
        max_results = st.slider("取得件数上限", 10, 200, 50, 10)

    # 入力エリアの動的変更
    input_data = ""
    if search_mode in ["JANコードリスト", "ASINリスト"]:
        input_data = st.text_area(f"{search_mode}を入力 (1行に1つ)", height=150)
    else:
        input_data = st.text_input(f"{search_mode} キーワードを入力")

    # 実行ボタン
    if st.button("検索開始", type="primary"):
        if not (lwa_app_id and lwa_client_secret and refresh_token):
            st.error("左側のサイドバーでAPI認証情報を設定してください。")
            return
        
        if not input_data:
            st.warning("検索キーワードまたはリストを入力してください。")
            return

        # クレデンシャル作成
        credentials = {
            'refresh_token': refresh_token,
            'lwa_app_id': lwa_app_id,
            'lwa_client_secret': lwa_client_secret,
            'aws_access_key': aws_access_key,
            'aws_secret_key': aws_secret_key,
            'role_arn': st.secrets.get("ROLE_ARN", "") # 必要であれば入力項目追加
        }

        searcher = AmazonSearcher(credentials)
        target_asins = []

        # プログレス表示用コンテナ
        progress_bar = st.progress(0)
        status_text = st.empty()
        result_container = st.container()

        # 1. ASINリストの生成
        status_text.info("ASINリストを作成中...")
        
        if search_mode == "JANコードリスト":
            jan_list = [line.strip() for line in input_data.split('\n') if line.strip()]
            for i, jan in enumerate(jan_list):
                status_text.text(f"JAN変換中: {jan} ({i+1}/{len(jan_list)})")
                asin = searcher.search_by_jan(jan)
                if asin:
                    target_asins.append(asin)
                time.sleep(0.5)
                progress_bar.progress((i + 1) / len(jan_list) * 0.3) # 前半30%

        elif search_mode == "ASINリスト":
            target_asins = [line.strip() for line in input_data.split('\n') if line.strip()]
            progress_bar.progress(30)

        else: # ブランド または カテゴリ/キーワード
            target_asins = searcher.search_by_keywords(input_data, max_results)
            progress_bar.progress(30)

        if not target_asins:
            st.error("対象の商品が見つかりませんでした。")
            return

        st.success(f"{len(target_asins)} 件の商品ASINを特定しました。詳細情報を取得します...")
        
        # 2. 詳細情報の取得
        results = []
        
        # プレースホルダーにテーブルの枠だけ作っておく
        df_placeholder = st.empty()
        
        for i, asin in enumerate(target_asins):
            status_text.text(f"詳細データ取得中: {asin} ({i+1}/{len(target_asins)})")
            
            # API制限に達しないよう少し待機
            time.sleep(1.5) 
            
            detail = searcher.get_product_details(asin)
            if detail:
                results.append(detail)
            
            # 途中経過をデータフレームとして更新表示 (常時表示)
            if results:
                df_current = pd.DataFrame(results)
                # 表示用にカラムを整理
                display_cols = {
                    'title': '商品名', 'brand': 'ブランド', 'price_disp': '価格', 
                    'rank_disp': 'ランキング', 'category': 'カテゴリ',
                    'points': 'ポイント率', 'fee_rate': '手数料率', 'asin': 'ASIN'
                }
                df_show = df_current[display_cols.keys()].rename(columns=display_cols)
                df_placeholder.dataframe(df_show, use_container_width=True)

            # 進捗バー更新 (残り70%分)
            current_progress = 0.3 + ((i + 1) / len(target_asins) * 0.7)
            progress_bar.progress(min(current_progress, 1.0))

        status_text.success("データ取得完了！")
        progress_bar.progress(100)

        # 3. ダウンロード機能
        if results:
            df_final = pd.DataFrame(results)
            
            # ★追加: 不要な列（rank, price）をCSVから削除する
            # ※ rank_disp（ランキング表示用）や price_disp（価格表示用）は残ります
            df_final = df_final.drop(columns=['rank', 'price'], errors='ignore')

            # 日本時間の日付ファイル名
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
