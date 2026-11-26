# Logic Synthesis Studio & Industrial VCD Waveform Viewer

> **Trình tổng hợp mạch Logic (Quine-McCluskey + Petrick's Method), Phân tích PPA / Đường găng (Static Timing Analysis), Sinh mã Verilog-2001 & Trình phân tích dạng sóng `.vcd` kiểu GTKWave trên trình duyệt**
> Vận hành trực tiếp trên **GitHub Pages** thông qua **Pyodide v0.26.2 (WebAssembly)** kết hợp HTML5 / CSS3 / Vanilla ES6 — **0% backend server, 0% bước build `npm`**, lõi thuật toán viết **100% bằng Python Standard Library** (`itertools`, `collections`, `re`, `dataclasses`, `json`).
>
> **Live Web EDA Demo (GitHub Pages / Pyodide WASM):** https://bondomaster.github.io/logic-synthesis-studio/

---

## 1. Cấu trúc dự án

```text
logic-synthesis-studio/
├── .nojekyll                 # Cho phép GitHub Pages phục vụ nguyên vẹn thư mục ./src/
├── index.html                # Giao diện 3 khu vực chuẩn Synopsys / Vivado Dark (#121417)
├── style.css                 # Hệ màu EDA công nghiệp, ký hiệu cổng IEEE & bảng sóng GTKWave
├── app.js                    # Tải Pyodide WASM, vẽ mạch SVG Pan/Zoom, Canvas VCD & con trỏ Δt
├── src/
│   ├── __init__.py
│   └── eda_core.py           # Lõi Python thuần (Quine-McCluskey, Petrick, STA/PPA, Verilog, VCD)
└── tests/
    └── test_eda_core.py      # Kiểm thử tự động unittest cho toàn bộ thuật toán lõi
```

---

## 2. Tính năng kỹ thuật cốt lõi

1. **Tối giản hàm Boolean chính xác (1 – 8 biến)**:
   - Nhận biểu thức đại số (`F = A'B'C + A'BC + AB'C' + AB'C + ABC`, toán tử `'`, `~`, `!`, `+`, `|`, `*`, `&`, `^`) hoặc dạng chuẩn Minterm + Don't-Care (`m(1,3,4,5,7) + d(2)`), đồng bộ hai chiều với **Bảng chân trị tương tác** (`0` / `1` / `X`).
   - Kết hợp **Quine-McCluskey** (gom nhóm theo trọng số Hamming bằng mặt nạ bit nguyên) và **Petrick's Method** (nhân mệnh đề POS kèm luật hấp thụ $X + XY = X$) để giải triệt để cả các bảng Prime Implicant chu trình khép kín (Cyclic Core).
2. **Ánh xạ công nghệ, Phân tích PPA & Đường găng (STA)**:
   - Hỗ trợ chuyển đổi giữa thư viện **CMOS (`NOT=1.0ns`, `AND=2.0ns`, `OR=2.5ns`)** và **ASIC Universal (`NOT=1.0ns`, `NAND=1.5ns` theo định lý De Morgan)**.
   - Thống kê diện tích tương đương (`GE`), tỷ lệ tiết kiệm cổng/diện tích (%), truy vết ngược đường găng (**Critical Path Delay**) tô đỏ trực tiếp trên bản vẽ cổng ký hiệu chuẩn **IEEE Std 91**, và tính tần số xung nhịp tối đa:
     $$F_{\max} = \frac{1000}{T_{\text{crit (ns)}}} \text{ (MHz)}$$
3. **Tự động sinh mã Verilog-2001 (`.v`) & Self-Checking Testbench (`_tb.v`)**:
   - Sinh module cấu trúc cổng kèm độ trễ `#(delay)` và Testbench tự động quét vector kiểm thử có sẵn `$dumpfile` & `$dumpvars` tương thích trực tiếp với **Icarus Verilog (`iverilog`)** và **ModelSim**.
4. **Mô phỏng trễ cổng 4 mức (`0`, `1`, `x`, `z`) & Trình đọc sóng `.vcd` chuẩn IEEE 1364**:
   - Bộ parser Regex đọc mọi file `.vcd` chuẩn IEEE 1364 (hỗ trợ đa tầng `$scope`, tín hiệu đơn bit `0/1/x/z` và bus nhiều bit `b1010...` hiển thị dạng hình lục giác chứa mã Hex).
   - Hỗ trợ lăn chuột **Zoom In/Out** theo vị trí con trỏ thời gian, bắt dính sườn xung (**Snap Edge**), và đặt **Cursor A / Cursor B** tự động đo độ lệch $\Delta t$ và tần số tương đương $1/|\Delta t|$.

---

## 3. Hướng dẫn chạy & Triển khai GitHub Pages

### Chạy cục bộ
```bash
cd d:\msrv\logic-synthesis-studio

# Chạy kiểm thử tự động lõi Python
python -m unittest discover -s tests -v

# Mở máy chủ tĩnh để trải nghiệm giao diện WebAssembly
python -m http.server 8000
# Truy cập: http://localhost:8000
```

### Triển khai GitHub Pages
Đẩy toàn bộ thư mục lên nhánh `main` trên GitHub và bật **Settings → Pages → Deploy from a branch (`main` / `/root`)**. Toàn bộ đường dẫn `./src/eda_core.py`, `./style.css`, `./app.js` đều là đường dẫn tương đối nên hoạt động ngay tại `https://<username>.github.io/<repo>/`.
