############## JRETSAnnounceExpansion v0.1 README ##############  

<img width="1915" height="821" alt="JRETSAnnounceExpansion" src="https://github.com/user-attachments/assets/4ac4f760-ee2a-487d-ab6f-0e71203a0d00" />

  
1　はじめに  
JRETSAnnounceExpansion（以下「本アプリ」といいます）をダウンロードしていただき、ありがとうございます。  
ご使用になる前に、必ず本ReadMeをご一読ください。  
  
2　本アプリの特徴  
本プロジェクトは、JRETSの画面をリアルタイムに監視し、OCR（光学文字認識）を用いて走行状態を解析、特定の条件を満たした際にキー入力を代行するシステムです。  
本アプリには、東海道線普通熱海行き（東京ー品川）・京浜東北線快速大船行き（大宮ー大船）・山手線内回り（大崎ー大崎）のシナリオが同梱されています。  
本アプリは次駅案内と到着案内、及び駅名連呼を自動化します。駅メロおよび発車放送は自動化されません。  
※本アプリは開発中です。思った通りの動作をしない場合があります。(OCRの誤認識が起きることがあります)  
※実行時にSmartscreenの警告が出ることがあります。ウイルスは含まれていませんが自己責任で実行してください。  
  
3　本アプリをご使用になる前に  
(1) 本アプリを使用するには別途TrainAnnouncePlayerをインストールしている必要があります。  
お持ちでない方は、https://github.com/Goma-Abura-lab/TrainAnnouncePlayer　からダウンロードしてください。  
なお、TrainAnnouncePlayer v1.0と本アプリは互換性がありません。  
必ず、最新版のTrainAnnouncePlayer(v1.1以降)をダウンロードしてください。  
(2) JRETSの設定画面で以下の設定をしてください。  
・設定→システム→言語→「日本語」を選択  
・設定→表示→全ての要素につき、表示するを選択  
  
4　本アプリのアルゴリズム解説  
本アプリは、device-high様の素晴らしいアプリ、jrets-soundplayからインスパイアを得て開発されました。この場をお借りして感謝申し上げます。  
ただし、本プロジェクトのソースコードは、PyQt6やOpenCVなどの異なる技術スタックを用いて完全にゼロから設計・実装された独立した著作物であり、元のスクリプトからのコードの流用・複製は一切含んでいません。そのため、本リポジトリは純粋なMITライセンスのもとで公開します。  
  
以下に本アプリのアルゴリズムを詳細に解説します。  
(1) 画面監視のアルゴリズム  
システムは毎秒約1回のサイクルで以下の処理をループ実行しています。  
①画面キャプチャと領域切り出し（ROI）  
画面全体の解像度（4K, 2K, フルHD等）に依存しないよう、画面全体の横幅・縦幅に対するパーセンテージで以下の4つの領域を切り出しています。  
監視項目	役割	切り出し位置の詳細  
駅名領域	次駅名の取得	モニター右上の「次駅表示」部分。漢字やひらがなを認識します。  
距離領域	残り距離の取得	モニター中央右寄りの「残り距離（m）」の数字部分。「上二桁」を読み取る設定になっています。  
速度領域	停車位置判定	モニター右側の速度計数字部分。停車判定に使用します。  
距離領域の色	停車位置判定	停車位置目標のインジケータ部分。文字ではなく「緑色のピクセルが占める割合」を計算します。  
②OCR処理とデータ補正  
切り出した画像に対して Tesseract OCR を実行します。数値データ（距離・速度）については、誤読を防ぐための独自の補正ロジックを搭載しています。  
距離の推論アルゴリズム: OCRは数字を誤認識することがあります。例えば「11」を「1」と誤読することがあります。本システムでは直近の距離の変化を記録しており、例えば 12 → 1 → 10 と推移した場合、真ん中の1は誤読と判断し、前後の流れから11であると推測して処理を継続します。  
  
(2) 条件判定とイベント発火  
読み取った駅名・距離・速度・距離領域の色の4つのデータを、設定ファイル（JSON）に記述された条件と照合します。  
  
各イベントには以下の条件が設定されています。  
・次駅放送および到着放送:  
①現在の次駅名が指定の駅であるか（曖昧一致により、多少の誤読は許容します）。  
②指定された距離（m）以下になったか。  
・駅名連呼:  
①速度が0.0kmになったか  
②距離領域のピクセルの色を解析し、一定割合（5%以上）が緑色になったか。（JRETSでは停車可能距離に達すると距離領域の文字が緑色になります。そこで速度が0.0kmになったかつ、距離領域のピクセルの色を解析し、一定割合（5%以上）が緑色になった瞬間に停車位置に正しく停まったと判定します。戸閉灯を監視していません）  
  
これら全ての条件が真となった瞬間に、システムは keyboard.send() コマンドを実行し、設定されたキー（デフォルトでは'0'）を送信します。  
これにより、TrainAnnouncePlayer側で放送が開始されます。  
これらのアルゴリズムによりTrainAnnouncePlayerで行う放送操作を自動化することができます。  
  
(3) 誤動作防止  
TrainAnnouncePlayerの誤作動を防ぐため、一度条件を満たすと、原則として60秒間は次のイベントの条件を満たしたとしてもキーを送信しません。  
例えば、浦和駅まで1400mの地点で放送を流し、浦和駅では駅名連呼をせずに、次の南浦和駅で1200mの地点で放送を流したいとします。  
すると、曖昧一致によりOCRが正しく認識されていても「南浦和」と「浦和」はアプリの認識では同一駅になります。  
そうすると、浦和駅まで1200mの地点でイベントの条件を満たしてしまうのでもう一度キーが送信されてしまい、TrainAnnouncePlayerの放送は止まってしまいます。  
そこで、誤動作防止のため前のイベントの条件を満たしてから60秒間は次のイベントの条件を満たしたとしてもキーを送信しません。  
ただし、同じ駅内で連続するイベント（例えば川口駅の次駅放送と到着放送、南浦和駅の次駅放送と駅名連呼）や駅名連呼と次の駅の次駅放送の場合は、誤動作防止はありません。  
  
(4)設定項目（JSON）の例  
{  
  "type": "distance",  
  "title": "浦和次駅案内",  
  "station": "浦和",  
  "trigger_distance": "12",  
  "arrive_key": "0"  
}  
type: で、次駅放送/到着放送か、駅名連呼かを指定する。  
title: でアプリのUI上に表示されるイベントの名前を指定する。  
station: "浦和" を認識し、  
trigger_distance: 距離が "12" 以下（実距離は1200m）になった時、  
arrive_key: キー "0" を送信する。  
  
  
5　アドオン制作について  
本アプリは、JSONファイルでシナリオを作成し、キーを送信する仕組みとなっています。  
そのため、JSONファイルを追加することで、簡単にアドオンを制作することができます。ぜひ、お好みの路線を追加してお楽しみください。  
  
しかし、JSONファイルを手作業で作成するのは手間のかかる作業です。そこで、本アプリ用のJSONファイルを作成するための「JRETSAnnounceExpansion editor」を同梱しました。  
これにより、コーディングの知識がない方でも簡単にシナリオを作成することができます。  
  
アドオン制作について詳しくは「JRETSAnnounceExpansion editor使用マニュアル」をご覧ください。(※突貫工事のため、使用マニュアルが完成しないままの公開となります。使い方は同梱のjsonファイルなどを見ながらご確認ください。いずれ完成させて公開します)  
  
6　今後の展望  
本アプリは開発中なので誤作動がまだまだ起きることがあります。  
誤作動を減らせるように頑張りたいです  
他にもたくさんシナリオを追加したいですね。  
  
7　サポートについて  
個別の操作方法等のサポートについてはお答えすることができませんのでご了承ください。  
バグが発生している場合は、Git Hubにてお知らせください。  
路線のリクエストはしていただいても構いませんが、何のお約束もすることはできません。返信も致しません。  
  
8　権利関係  
This software includes the work that is distributed in the Apache License 2.0.  
JRETSAnnounceExpansion\Tesseract-OCR\LICENSE\LICENSE.txt is a copy of Apache License 2.0.  
このソフトウェアは、Apache 2.0ライセンスで配布されている製作物が含まれています。  
JRETSAnnounceExpansion\Tesseract-OCR\LICENSE\LICENSE.txtはApache 2.0ライセンスのコピーです。  
  
9　ライセンス  
本アプリのソースコード(main.py)はMITライセンスとします。  
なお、本アプリケーションのロゴ及びエディタアプリの著作権は (c) 2026 GomaAbura に帰属し、MITライセンスの対象外です。無断転載・複製を禁じます。  
以下にMITライセンスの全文を記載します。  
  
The MIT License  
  
Copyright (c) 2026 GomaAbura  
  
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:  
  
The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.  
  
THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.  
  
10　謝辞  
私自身はecho hello world!しか書けないプログラミング知識のない人間です。GeminiとClaudeに細かく聞きながらプロンプトを書かせて、Google Antigravityでコーディングしました。要はVibe Codingです。動作確認やウイルスチェック、コード整理は行っていますが、予期しない不具合が残っている可能性があります。問題を発見した場合は、ご報告よろしくお願いします。  
素晴らしいツールを作ってくださったGemini、Claude、Google Antigravityの開発者の皆様に深く感謝いたします。  
また、インスパイアを得させていただきましたdevice-high様には重ねて感謝申し上げます。  
最後に、JR東日本トレインシミュレーターという稀代のトレインシミュレータを開発されている皆様に心より感謝いたします。  
  
11　クレジット  
開発:ごま油  
ホームページ（https://ilovegt.blog.jp/）  
メールアドレス（gomaabura@protonmail.com）  
  
本アプリはGoogle Antigravityを使用して開発しました。  
本アプリのロゴはChatGPT Images 2.0を使用して作成しました。  
  
本アプリ（プログラム部分およびロゴ）の著作権はごま油に帰属します。  
c 2026 ごま油 All Rights Reserved.  
