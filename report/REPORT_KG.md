# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Trần Anh Vũ  **MSSV:** 2A202602570  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

Ontology: **tự thiết kế** (`report/ONTOLOGY.md`). Baseline ontology gợi ý chạy bằng `KG_ONTOLOGY=hint python bench_kg.py --judge --out ket_qua_benchmark_kg.hint.txt`, cùng code, cùng provider. Cả hai file dùng `openai:gpt-4o-mini` + `openai:text-embedding-3-small`, top_k=3, chunk_size=800, 176 chunk.

## 1. Chi phí (10 điểm)

Trích từ `ket_qua_benchmark_kg.txt` (KG: 211 nodes / 450 rels):

```
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112     65.0
graph       196     95158     5049   0.01001    147.0

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       47   0.00013     1.55
graph       1.00   2.00     1838       83   0.00032     2.15
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00112 | 0.01001 | ×8.94 |
| Indexing giây | 65.0 | 147.0 | ×2.26 |
| Mỗi câu: USD | 0.00013 | 0.00032 | ×2.46 |
| Mỗi câu: giây | 1.55 | 2.15 | ×1.39 |
| Mỗi câu: in_tok | 694 | 1838 | ×2.65 |

**Chi phí tăng thêm đến từ đâu?**
> **Indexing:** graph = flat + dựng KG. Phần KG là 20 lần gọi LLM trích xuất (196 − 176), tức 39 086 in_tok (= 95 158 − 56 072) + 5 049 out_tok = **0.00889 USD**, chiếm ~89 % tiền indexing của graph, và ~82 giây. Phần luật dựng bằng regex nên không tốn token. Out_tok (giá gấp 4 lần in_tok với gpt-4o-mini) là JSON trích xuất. **Mỗi câu hỏi:** prompt dài thêm ~1 144 token dữ kiện graph (1838 − 694); thời gian thêm ~0.6 s đến từ 4–5 truy vấn Cypher và output dài hơn (83 vs 47 token, vì câu trả lời nêu Điều/khoản).
>
> **So với ontology gợi ý** (`ket_qua_benchmark_kg.hint.txt`: graph indexing 0.00938 USD, query 3 991 in_tok / 0.00063 USD mỗi câu): ontology của tôi tốn thêm 0.00063 USD lúc dựng (prompt trích xuất dài hơn) nhưng **rẻ hơn một nửa mỗi câu**, vì chỉ đưa `penalty` của các khoản được chọn (khoản 1, khoản nặng nhất, khoản khớp ngưỡng) thay vì toàn văn khoản. Hòa vốn sau ~2 câu hỏi (0.00063 / 0.00031).
>
> **Điểm hòa vốn so với Flat**, tính theo *chi phí cho mỗi câu trả lời đúng đủ (judge = 2)*: Flat đúng đủ 2/6 câu, Graph 6/6. Với N câu cùng phân bố loại câu như benchmark: Flat = (0.00112 + 0.00013·N) / (N/3), Graph = (0.01001 + 0.00032·N) / N. Hai bên bằng nhau khi N ≈ **95 câu**. Trên 95 câu, GraphRAG rẻ hơn tính trên mỗi câu đúng; dưới mức đó, chi phí trích xuất một lần chưa được bù. Tính theo USD tuyệt đối thì Graph luôn đắt hơn (×2.46 mỗi câu), nhưng Flat **không mua được** đáp án xuyên KB ở bất kỳ mức giá nào (Q3, Q4 = 0 điểm).

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Định nghĩa nằm gọn trong 1 chunk; graph chỉ thêm `Term 'Tiền chất'` và trích được "Điều 2 Luật PCMT khoản 4" (ontology gợi ý ở câu này còn thua: 0.00 / 0) |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên 2 bị cáo tử hình nằm trong cùng một chunk tin; graph không cần thiết |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Top-3 chunk không có Điều 251, Flat trả "Không đủ thông tin"; graph đi `Person→Case→Crime←Article→Clause 1` |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Graph map alias "Hoàng Nato" → `Dương Minh Tuấn` → Điều 255 và lấy khoản có `severity` cao nhất (tù chung thân) |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 1.00 / 2 | Graph | Flat có chunk luật nhưng nói "khoản b)", không nêu Điều 250; graph khớp 9 600 g MDMA vào `THRESHOLD` ≥100 g → khoản 4 điểm b |
| Q6 | aggregation | 0.00 / 1 | 1.00 / 2 | Graph | Top-3 chunk chỉ phủ được vài bài; graph lấy mọi `Case-[:INVOLVES]->(:Substance {name:'MDMA'})` trên toàn KB |

**Quy luật:** câu **single-hop** (đáp án trong 1 đoạn, Q1, Q2) thì hai pipeline hòa, và Graph chỉ tốn thêm tiền. Câu **cross-kb / multi-hop** (Q3–Q5) thì Graph thắng tuyệt đối, vì không chunk nào chứa cả "người + mức án" lẫn "Điều + khung". Câu **aggregation** (Q6) thì Graph thắng vì top-k bị giới hạn số chunk, còn Cypher thì quét toàn graph; tuy vậy kết quả vẫn phụ thuộc LLM có liệt kê đủ hay không (E5).

## 3. Phân tích lỗi (20 điểm)

Bằng chứng Cypher được chạy trên graph của lần `--judge` cuối (211 node / 450 cạnh), trừ khi ghi "hint", nghĩa là graph của `KG_ONTOLOGY=hint` (206 node / 385 cạnh).

### Lỗi E2: Thiếu ngữ cảnh luật (sai khung hình phạt tối đa)

- **Hiện tượng:** với ontology gợi ý, GraphRAG trả lời sai mức phạt tối đa của Q4 dù graph có đủ Điều 255 với cả 5 khoản.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q4, pipeline graph (recall 0.67, judge 1):

```
Giang hồ 'Hoàng Nato' bị bắt về hành vi tổ chức sử dụng trái phép chất ma túy. Hành vi này có thể bị phạt tù tối đa 07 năm theo Điều 255 BLHS khoản 1.
```

Các khoản của Điều 255 và chất mà chúng nhắc tới (truy vấn cho graph hint; kết quả kiểm lại bằng chính hàm regex `parse_law_article` của ontology gợi ý: mọi khoản đều có `substances = []`):

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl:Clause)
OPTIONAL MATCH (cl)-[:MENTIONS]->(s:Substance)
RETURN cl.number, cl.penalty, collect(s.name) AS subs ORDER BY cl.number
```

```
1  phạt tù từ 02 năm đến 07 năm                 []
2  phạt tù từ 07 năm đến 15 năm                 []
3  phạt tù từ 15 năm đến 20 năm                 []
4  phạt tù 20 năm hoặc tù chung thân            []
5  phạt tiền từ 50.000.000 đồng đến 500.000.000 đồng, ...   []
```

Quy tắc lọc ở Bước 5 là "khoản 1 **hoặc** khoản `MENTIONS` một chất mà vụ `INVOLVES`". Điều 255 (tổ chức sử dụng) không có khoản nào nhắc tên chất, nên chỉ khoản 1 (tối đa 07 năm) lọt vào prompt. LLM trả lời đúng theo ngữ cảnh nó được đưa, nhưng ngữ cảnh thiếu.
- **Nguyên nhân:** **Cypher KG-3 + thiết kế ontology**. Quy tắc lọc khoản giả định khung nặng hơn luôn đi kèm khối lượng chất, điều này chỉ đúng với các Điều 248–252. Ontology gợi ý cũng không có thuộc tính nào cho biết khoản nào là khung nặng nhất.
- **Đề xuất sửa (đã làm):** thêm `Clause.severity` (tính bằng regex `penalty_severity` trong `src/graph.py`: tử hình 100, chung thân 50, còn lại = số năm tù tối đa), và KG-3 luôn lấy thêm `cl.severity = max(severity)` của Điều. Kết quả trong `ket_qua_benchmark_kg.txt`, Q4 graph, recall 1.00, judge 2: *"…có thể bị phạt tù tối đa 20 năm hoặc tù chung thân theo Điều 255 Bộ luật Hình sự."* Đánh đổi: thêm 1 dòng ~25 token mỗi Điều. Đồng thời thay toàn văn khoản bằng `penalty`, nên in_tok mỗi câu vẫn giảm 3 991 → 1 838. Cũng cùng ý đó, ngưỡng `THRESHOLD` thay cho `MENTIONS` giúp chọn **đúng một** khoản theo khối lượng:

```cypher
MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
      -[t:THRESHOLD]->(s:Substance)<-[i:INVOLVES]-(k)
WHERE i.grams >= t.min_g AND (t.max_g IS NULL OR i.grams < t.max_g)
RETURN k.name, s.name, i.amount, a.id, cl.number, t.point, cl.penalty
```

```
Vụ phát hiện 20kg ma túy tại Phú Quốc     | Methamphetamine | 20kg      | Điều 249 BLHS | 4 | b | phạt tù từ 15 năm đến 20 năm hoặc tù chung thân
Vụ tổ chức sử dụng ma túy tại Sầm Sơn     | MDMA            | 0,686g    | Điều 249 BLHS | 1 | c | phạt tù từ 01 năm đến 05 năm
Vụ vận chuyển ma túy của Cái Quang Huy    | MDMA            | hơn 9,6kg | Điều 250 BLHS | 4 | b | phạt tù 20 năm, tù chung thân hoặc tử hình
Vụ vận chuyển ma túy từ Đức về Việt Nam   | MDMA            | hơn 9,6kg | Điều 250 BLHS | 4 | b | phạt tù 20 năm, tù chung thân hoặc tử hình
Vụ vận chuyển 840kg ma túy đá tại Campuchia | Methamphetamine | 840kg   | Điều 250 BLHS | 4 | b | phạt tù 20 năm, tù chung thân hoặc tử hình
```

(Kết quả trên lấy từ lần chạy trước khi thêm bộ lọc placeholder, xem mục "Vấn đề gặp phải"; các dòng ngưỡng không đổi.)

### Lỗi E3: Trùng thực thể

- **Hiện tượng:** cùng một chất ngoài đời thành nhiều node `Substance`; xuất hiện cả node không phải chất cụ thể. Thêm vào đó, cùng một vụ (Hoàng Nato) thành nhiều node `Case`.
- **Bằng chứng:** graph hint:

```cypher
MATCH (s:Substance) RETURN s.name ORDER BY toLower(s.name)
```

```
Amphetamine, chất ma túy, Cocaine, côca, cần sa, etomidate, Heroine, Ketamine, ketamine, ma túy,
ma túy tổng hợp, MDMA, Methamphetamine, methamphetamine, thuốc lắc, thuốc phiện, XLR-11      (17 node)
```

`Ketamine`/`ketamine` và `Methamphetamine`/`methamphetamine` khác nhau chỉ ở chữ hoa; `thuốc lắc` chính là MDMA (vụ Hoàng Nato `INVOLVES` `thuốc lắc` nên không xuất hiện khi hỏi về MDMA); `ma túy`, `chất ma túy`, `ma túy tổng hợp` không phải chất cụ thể. Case trùng (graph hint):

```cypher
MATCH (k:Case) WHERE k.name CONTAINS 'Hoàng Nato' RETURN k.name, k.doc_id
```

```
Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây ma túy | news-100260920221957595
Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato'           | news-100260922111804786
Vụ sử dụng ma túy etomidate của Hoàng Nato và Phan Kim Nhi           | news-100260924095400982
Vụ bắt 'Hoàng Nato' và triệt phá 8 đường dây ma túy                  | news-100260925144412498
```

- **Nguyên nhân:** **thiết kế ontology (khóa định danh)**. `MERGE (sub:Substance {name: s.name})` lấy nguyên chuỗi LLM trả về: Neo4j so khớp phân biệt hoa thường, và không có bước chuẩn hóa hay bảng đồng nghĩa. `Case` khóa theo tên LLM đặt, nên mỗi bài đặt một tên khác cho cùng một vụ.
- **Đề xuất sửa:** (đã làm) `canonical_substance` gồm bảng `SUBSTANCE_ALIASES`, bỏ tên chung, `link_entity(normalize=normalize_substance)`. Sau sửa:

```
MATCH (s:Substance) RETURN s.name ORDER BY toLower(s.name)
Amphetamine, Cocaine, côca, cần sa, etomidate, Heroine, Ketamine, MDMA, Methamphetamine, thuốc phiện, XLR-11   (11 node)
```

Vụ Hoàng Nato giờ `INVOLVES` → `MDMA`, nên được tìm thấy khi hỏi về MDMA. Với `Case`, tôi chọn khóa `doc_id#i` (tất định, không gộp nhầm), và nối các bài về cùng một vụ qua `Person` có khóa chuẩn hóa:

```cypher
MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WITH p, count(DISTINCT k) AS n WHERE n > 1
RETURN p.name, p.aliases, n
```

```
Dương Minh Tuấn  ["Hoàng Nato"]                                   4
Phan Kim Nhi     ["Phannhibeauty", "TikToker Phannhibeauty"]        3
Nguyễn Minh Đức  ["Đức Cộng"]                                       2
Nguyễn Thị Mai Anh ["bà 'trùm'", "bà trùm"]                         2
Cái Quang Huy    []                                                 2
Lê Văn Đông      []                                                 2
```

Còn lại (chưa sửa): 4 node `Case` cho 1 vụ Hoàng Nato. Muốn gộp hẳn thì cần bước entity resolution xuyên bài (ví dụ cạnh `SAME_AS` khi hai Case có chung ≥1 bị can + cùng tội danh + ngày gần nhau). Đánh đổi: thêm một pass Cypher và rủi ro gộp nhầm hai vụ khác nhau của cùng một người.

### Lỗi E5: LLM lệch với graph (câu aggregation Q6)

- **Hiện tượng:** graph có đủ các vụ liên quan MDMA, nhưng câu trả lời của GraphRAG liệt kê không đủ, và kết quả khác nhau giữa các lần chạy.
- **Bằng chứng:** Cypher trả lời thẳng Q6:

```cypher
MATCH (k:Case)-[r:INVOLVES]->(:Substance {name:'MDMA'}) RETURN k.name, k.doc_id, r.amount ORDER BY k.doc_id
```

```
Vụ vận chuyển ma túy từ Đức về Việt Nam                               | news-100260917203001265 | hơn 9,6kg
Vụ vận chuyển ma túy của Cái Quang Huy                                | news-100260918080821054 | hơn 9,6kg
Vụ góp tiền mua ma túy tại Hà Nội                                     | news-100260918080821054 | 5 viên
Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây ma túy | news-100260920221957595 |
Vụ án tại Viện Pháp y tâm thần Trung ương                             | news-100260924105118645 |
Vụ tổ chức sử dụng ma túy tại Sầm Sơn                                 | news-100260930085028036 | 0,686g
```

Câu trả lời GraphRAG trong `ket_qua_benchmark_kg.txt` (Q6, recall 1.00, judge 2) liệt kê 3 vụ chính (Cái Quang Huy, Lê Minh Thành, Sầm Sơn), sau đó *"Ngoài ra, MDMA cũng được đề cập trong vụ án tại Viện Pháp y tâm thần Trung ương…"*. **Thiếu** vụ Hoàng Nato (graph có `INVOLVES MDMA`). Hai dòng Cái Quang Huy là **thừa** (một vụ, hai Case), và LLM đã gộp đúng. Ở lần `--judge` trước đó (cùng ontology, trước khi thêm bộ lọc placeholder), Q6 graph được recall 0.67, judge 1 và **bỏ hẳn** vụ Pháp y:

```
1. **Vụ vận chuyển ma túy từ Đức về Việt Nam**: Cái Quang Huy bị cáo buộc vận chuyển hơn 9,6kg MDMA.
2. **Vụ góp tiền mua ma túy cho tiệc sinh nhật**: ... 5 viên MDMA.
3. **Vụ tổ chức sử dụng ma túy tại Sầm Sơn**: Lê Văn Đông bị cáo buộc tàng trữ 0,686g MDMA.
```

Baseline hint (`ket_qua_benchmark_kg.hint.txt`, Q6 graph) cũng thiếu đúng vụ Pháp y. Như vậy lỗi **không** ổn định giữa các lần chạy.
- **Nguyên nhân:** **prompt trả lời + phép đo ngữ cảnh**. Dữ kiện graph được đưa dưới dạng văn xuôi, xen với 3 chunk vector; chunk nói rất chi tiết về 3 vụ, nên LLM "neo" vào chunk và coi dòng graph về các vụ còn lại (summary không nêu khối lượng MDMA) là phụ. Ngoài ra `INVOLVES.amount` rỗng ở vụ Hoàng Nato/Pháp y khiến dòng dữ kiện trông "yếu". Nhiệt độ sampling của LLM làm kết quả dao động.
- **Đề xuất sửa:** với câu aggregation (nhận diện bằng mẫu "những vụ nào", "bao nhiêu vụ"), KG-3 trả về một **danh sách đánh số** đã khử trùng theo `Person` (không phải văn xuôi), và `GRAPH_PROMPT` thêm "liệt kê **đủ mọi** vụ trong danh sách dữ kiện graph". Hoặc trả lời thẳng bằng Cypher, LLM chỉ diễn đạt lại. Đánh đổi: cần bộ phân loại câu hỏi (regex rẻ, hoặc thêm 1 lần gọi LLM); trả lời bằng template thì kém tự nhiên.

### Lỗi E1: Cầu nối gãy

- **Hiện tượng:** có vụ án ma túy không nối được sang luật.
- **Bằng chứng:**

```cypher
MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.name, k.doc_id, k.stage
```

```
Vụ bắt giữ Nguyễn Minh Đức                 | news-100260918220613301 | bắt giữ
Vụ án tại Viện Pháp y tâm thần Trung ương  | news-100260924105118645 | xét xử sơ thẩm
Vụ tông cảnh sát giao thông ở An Giang      | news-100260926112415229 | khởi tố
```

Mở bài gốc: (1) `news-100260926112415229`: *"khởi tố bị can… Nguyễn Minh Nhân… về hành vi chống người thi hành công vụ"*, ngoài Chương XX, nên gãy là **hợp lý**. (2) `news-100260918220613301` có tiêu đề về công đoàn phòng chống ma túy, nhưng kèm đoạn tin *"Công an tỉnh Ninh Bình vừa bắt giữ khẩn cấp Nguyễn Minh Đức, tức 'Đức Cộng'…"*, không nêu tội danh. Không nối là chấp nhận được (thông tin không có trong bài); bài chính về Đức Cộng (`news-100260924101703641`) có nối. (3) `news-100260924105118645` (Pháp y tâm thần): bài nêu vợ chồng bị can Nguyễn Thị Mai Anh và Lê Văn Đông nhiều lần ra khỏi viện; *"Đông mang theo loa, bàn DJ và ma túy để tổ chức 'bay lắc'"* ở Sầm Sơn. Vụ này **nên** nối tới "tổ chức sử dụng trái phép chất ma túy" (Điều 255). Ở lần chạy hint, chính vụ này có `CHARGED_WITH`, tức là gãy do trích xuất không ổn định. Cùng lúc E6 xuất hiện: `MATCH (p:Person)-[r:INVOLVED_IN]->(k) WHERE r.charge = ''` cho thấy `Nguyễn Thị Mai Anh` và `Lê Văn Đông` (vai trò `bị cáo`) không có tội danh trong vụ này. Đây là lỗi trích xuất. Ngược lại, `Ngô Việt Dũng`, `Cao Thị Bích Hằng`, `Trần Quốc An` (vai trò `người liên quan`) không có tội danh là **hợp lý**.
- **Nguyên nhân:** **prompt trích xuất**. Bài dài, chủ đề chính là việc "chạy" giám định tâm thần để vào chữa bệnh bắt buộc; hành vi ma túy chỉ được kể như tình tiết ("tổ chức 'bay lắc'"), không có câu nêu tội danh. Prompt bắt "chọn đúng nguyên văn từ DANH SÁCH TỘI DANH", nên khi bài không viết đúng chữ, LLM để trống thay vì map. `link_entity` không giúp được vì đầu vào đã rỗng.
- **Đề xuất sửa:** fallback trong `extract_news_cases_custom`: nếu `charges` rỗng thì chạy `link_entity` trên các cụm `tội [^,.;]+` và động từ hành vi ("tổ chức sử dụng", "vận chuyển", "mua bán") regex được từ chính bài, với cutoff 0.8. Đánh đổi: tăng rủi ro nối nhầm khi bài *nhắc* một tội nhưng không phải tội của vụ (bài tuyên truyền), nên chỉ áp dụng cho Case có `stage` ∈ {khởi tố, truy tố, xét xử}.

### (Phụ) Lỗi E4: Phép đo sai

`ket_qua_benchmark_kg.txt`, Q6 **flat**: recall 0.00 nhưng judge 1. Câu trả lời viết *"Vụ việc của Thành liên quan đến 5 viên nén… MDMA"*, *"Vụ việc của Đông… 0,686g MDMA"*: đúng một phần nội dung, nhưng `must_include` đòi chuỗi đầy đủ "Lê Minh Thành", "Cái Quang Huy", "Pháp y tâm thần", nên recall chấm 0. Judge (1 = đúng một phần) hợp lý hơn. Ngược lại, Q5 flat được judge 1 dù câu trả lời ghi "khoản b)" (sai: phải là khoản 4 điểm b) và không nêu Điều. Recall 0.60 phản ánh sát hơn. Kết luận: recall bị đánh lừa bởi cách viết tên, judge dễ dãi với sai chi tiết pháp lý; nên báo cáo cả hai và đọc nguyên văn.

## 4. Kết luận (5 điểm)

> **Nên dùng KG khi** câu hỏi cần ghép dữ kiện từ ≥ 2 nguồn không cùng đoạn văn (người/mức án ở tin + Điều/khung ở luật), cần chọn đúng nhánh theo điều kiện số (khối lượng → khoản), hoặc cần quét toàn bộ KB (aggregation). Trên 4 câu loại đó (Q3–Q6), GraphRAG đạt recall 1.00 / judge 2 ở cả 4, còn Flat chỉ được recall trung bình 0.15 (0, 0, 0.60, 0) và judge 0.5. Chi phí: indexing ×8.94 (0.01001 vs 0.00112 USD, gần như toàn bộ là 20 lần gọi LLM trích xuất), mỗi câu ×2.46 USD và ×1.39 thời gian. Tính theo chi phí cho mỗi câu đúng đủ, KG hòa vốn sau ~95 câu hỏi với phân bố câu như benchmark.
>
> **Flat RAG là đủ khi** đáp án nằm gọn trong một đoạn (Q1, Q2: hai bên đều 1.00 / 2), hoặc khi KB thay đổi liên tục và số câu hỏi ít (< ~100 câu cho mỗi lần dựng lại KG): khi đó chi phí trích xuất không được bù.
>
> **Điều kiện để KG đáng tiền** (rút ra từ số liệu tự đo): (1) một nửa KB có cấu trúc đều để trích bằng regex miễn phí (luật: 0 token); (2) có node cầu nối được chuẩn hóa chặt. Ontology gợi ý với cầu nối lỏng chỉ đạt recall 0.67 / judge 1.17 và còn **thua** Flat ở Q1; (3) ontology được thiết kế theo competency questions. Cùng dữ liệu và model, chỉ đổi ontology đã nâng judge 1.17 → 2.00 và *giảm* chi phí mỗi câu 0.00063 → 0.00032 USD. Thiết kế ontology quan trọng hơn bản thân việc "có graph".

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.09s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openai:gpt-4o-mini | embedding = openai:text-embedding-3-small
[OK] KG-2 build_graph: 163 node / 366 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 8 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00079. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png` (chụp trên graph dựng bằng `python bench_kg.py --build`: 208 node / 448 cạnh).
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy**

## Vấn đề gặp phải (không tính điểm)

> Lần `--judge` đầu tiên với ontology tự thiết kế làm lộ một lỗi: LLM chép nguyên chữ "chuỗi rỗng" trong prompt (`"chuỗi rỗng nếu không rõ"`) thành giá trị, sinh ra `(:Person {name:'chuỗi rỗng'})` và `(:Substance {name:'chuỗi rỗng'})`. Đã sửa bằng hàm `_clean` (lọc "chuỗi rỗng", "không rõ"… trong code), rồi chạy lại `--judge`. `ket_qua_benchmark_kg.txt` là kết quả của lần chạy sau khi sửa. Kiểm tra: `MATCH (p:Person) WHERE p.name CONTAINS 'chuỗi' RETURN count(p)` → `0`.
