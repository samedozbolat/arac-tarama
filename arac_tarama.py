#!/usr/bin/env python3
"""
Araç Tarama - Mini R60 ve BMW G20 için canlı veri ve hata hafızası paneli.

Çalıştırma : python arac_tarama.py        (ya da "Araç Tarama.bat" çift tık)
Araçsız    : açılış ekranında "Deneme modu" kutusunu işaretle.

MHD WiFi adaptörü üzerinden çalışır. Yalnızca okuma yapar: canlı değerler
ve hata hafızası. Hata silmez, kodlama yapmaz, beyne hiçbir şey yazmaz.
Kayıtlar "kayitlar" klasörüne yazılır (her bağlantı için ayrı CSV).
"""
import json
import math
import os
import queue
import random
import socket
import struct
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk

KLASOR = os.path.dirname(os.path.abspath(__file__))
KAYIT_KLASORU = os.path.join(KLASOR, "kayitlar")
VERI_KLASORU = os.path.join(KLASOR, "veri")
RESIM_KLASORU = os.path.join(KLASOR, "resimler")
ADAPTOR_IP = "192.168.4.1"


def tablo_yukle(kod):
    """BMW teşhis tanımlarından çıkarılmış ölçüm ve hata tablosu.
    olcum: kimlik -> [ad (Almanca), birim, tip, çarpan, ekle]
    hata : kaynak -> {kod: açıklama (Almanca)}"""
    try:
        with open(os.path.join(VERI_KLASORU, kod + ".json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"olcum": {}, "hata": {}}


def kart_resmi(kod, en, boy):
    """Araç kartının arka planı: resimler/<kod>.png (ya da .jpg/.webp).
    Resim karta sığacak şekilde kırpılır, yazı okunsun diye üstten ve alttan
    karartılır. Dosya ya da Pillow yoksa None döner (düz kart)."""
    for uzanti in (".png", ".jpg", ".jpeg", ".webp"):
        yol = os.path.join(RESIM_KLASORU, kod + uzanti)
        if os.path.exists(yol):
            break
    else:
        return None
    try:
        from PIL import Image, ImageTk
        r = Image.open(yol).convert("RGB")
        oran = max(en / r.width, boy / r.height)
        r = r.resize((round(r.width * oran), round(r.height * oran)),
                     Image.LANCZOS)
        sol, ust = (r.width - en) // 2, (r.height - boy) // 2
        r = r.crop((sol, ust, sol + en, ust + boy))
        maske = Image.new("L", (1, boy))
        for y in range(boy):
            ustten = max(0.0, 1 - y / (boy * 0.42))
            alttan = max(0.0, 1 - (boy - y) / (boy * 0.30))
            maske.putpixel((0, y), int(190 * max(ustten, alttan)))
        r.paste(Image.new("RGB", (en, boy), (10, 12, 16)), (0, 0),
                maske.resize((en, boy)))
        return ImageTk.PhotoImage(r)
    except Exception:
        return None


def ham_coz(tip, d):
    """Tablodaki veri tipine göre baytları sayıya çevirir."""
    n = int(tip[1])
    if len(d) < n:
        raise IndexError
    if tip[0] == "f":
        return struct.unpack(">f", bytes(d[:4]))[0]
    return int.from_bytes(bytes(d[:n]), "big", signed=tip[0] == "s")

ZEMIN, KUTU, CIZGI = "#14171c", "#1f242c", "#2c333d"
YAZI, SOLUK = "#e8eaed", "#8b95a3"
MAVI, SARI, KIRMIZI, YESIL = "#4da3ff", "#ffb454", "#ff5c5c", "#5fd38d"
FONT = "Segoe UI"


# ====================================================================
# Bağlantılar
# ====================================================================
class KwpBaglanti:
    """E-serisi dönemi (R60): ham TCP 23, BMW-FAST çerçevesi."""
    PORT = 23
    TESTER = 0xF1

    def __init__(self, ip=ADAPTOR_IP):
        self.sock = socket.create_connection((ip, self.PORT), 5)
        time.sleep(0.4)
        self.sock.settimeout(0.3)
        try:
            self.sock.recv(4096)                  # varsa açılış verisi
        except socket.timeout:
            pass
        self.tampon = b""

    @staticmethod
    def cerceve(hedef, kaynak, veri):
        n = len(veri)
        govde = ([0x80 | n, hedef, kaynak] if n <= 63
                 else [0x80, hedef, kaynak, n]) + list(veri)
        return bytes(govde + [sum(govde) & 0xFF])

    def _ayikla(self):
        t = self.tampon
        while len(t) >= 4:
            if not t[0] & 0x80:
                t = t[1:]
                continue
            n, bas = t[0] & 0x3F, 3
            if n == 0:
                n, bas = t[3], 4
            toplam = bas + n + 1
            if len(t) < toplam:
                break
            if sum(t[:toplam - 1]) & 0xFF == t[toplam - 1]:
                self.tampon = t[toplam:]
                return t[:toplam], t[bas:toplam - 1]
            t = t[1:]
        self.tampon = t
        return None, None

    def sor(self, ecu, veri, bekleme=0.6):
        giden = self.cerceve(ecu, self.TESTER, veri)
        self.sock.sendall(giden)
        son = time.time() + bekleme
        while True:
            c, v = self._ayikla()
            if c is None:
                kalan = son - time.time()
                if kalan <= 0:
                    return None
                self.sock.settimeout(kalan)
                try:
                    parca = self.sock.recv(4096)
                except socket.timeout:
                    return None
                if not parca:
                    raise ConnectionError("Adaptör bağlantıyı kapattı")
                self.tampon += parca
                continue
            if c == giden:
                continue                          # adaptörün yansıtması
            if len(v) >= 3 and v[0] == 0x7F and v[2] == 0x78:
                son = time.time() + 5.0           # "bekle, hazırlıyorum"
                continue
            return bytes(v)

    def kapat(self):
        try:
            self.sock.close()
        except OSError:
            pass


class HsfzBaglanti:
    """F/G-serisi (G20): BMW ENET, HSFZ çerçevesi, TCP 6801."""
    PORT = 6801
    TESTER = 0xF4

    def __init__(self, ip=ADAPTOR_IP):
        self.sock = socket.create_connection((ip, self.PORT), 5)

    @staticmethod
    def cerceve(kontrol, govde):
        return len(govde).to_bytes(4, "big") + kontrol.to_bytes(2, "big") \
            + bytes(govde)

    def _tam_oku(self, n, son):
        veri = b""
        while len(veri) < n:
            kalan = son - time.time()
            if kalan <= 0:
                raise socket.timeout()
            self.sock.settimeout(kalan)
            parca = self.sock.recv(n - len(veri))
            if not parca:
                raise ConnectionError("Adaptör bağlantıyı kapattı")
            veri += parca
        return veri

    def sor(self, ecu, veri, bekleme=1.0):
        self.sock.sendall(self.cerceve(1, [self.TESTER, ecu] + list(veri)))
        son = time.time() + bekleme
        try:
            while True:
                bas = self._tam_oku(6, son)
                uzunluk = int.from_bytes(bas[:4], "big")
                if uzunluk > 70000:
                    raise ConnectionError("Anlamsız çerçeve")
                kontrol = int.from_bytes(bas[4:], "big")
                govde = self._tam_oku(uzunluk, son)
                if kontrol == 2:
                    continue                      # geçit onayı
                if kontrol != 1:
                    return None                   # geçit hata bildirdi
                if len(govde) < 3 or govde[0] != ecu:
                    continue
                v = govde[2:]
                if len(v) >= 3 and v[0] == 0x7F and v[2] == 0x78:
                    son = time.time() + 5.0
                    continue
                return bytes(v)
        except socket.timeout:
            return None

    def kapat(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ====================================================================
# Araç tanımları
# ====================================================================
def u16(d):
    return d[0] * 256 + d[1]


def kelvin10(d):
    return u16(d) / 10 - 273.15


class P:
    """Bir canlı değer: nasıl istenir, nasıl çevrilir, nasıl gösterilir."""

    def __init__(self, anahtar, baslik, birim, bicim, istek, cevir, deneme=0):
        self.anahtar, self.baslik, self.birim = anahtar, baslik, birim
        self.bicim, self.istek, self.cevir = bicim, list(istek), cevir
        self.deneme = deneme
        self.onek = bytes([istek[0] + 0x40] + list(istek[1:]))

    def coz(self, cevap):
        if not cevap or cevap[:len(self.onek)] != self.onek:
            return None
        try:
            return self.cevir(cevap[len(self.onek):])
        except IndexError:
            return None


def r60_hata_oku(bag):
    c = bag.sor(0x12, [0x18, 0x02, 0xFF, 0xFF], 3)
    if not c or c[0] != 0x58:
        return None
    return [("Motor beyni", "%02X%02X" % (c[i], c[i + 1]), c[i + 2])
            for i in range(2, len(c) - 2, 3)]


def g20_hata_oku(bag):
    sonuc, okundu = [], False
    for ecu, ad in ((0x12, "Motor beyni"), (0x10, "Ana geçit")):
        c = bag.sor(ecu, [0x19, 0x02, 0x0C], 4)
        if not c or c[0] != 0x59:
            continue
        okundu = True
        k = c[3:]
        sonuc += [(ad, "%02X%02X%02X" % tuple(k[i:i + 3]), k[i + 3])
                  for i in range(0, len(k) - 3, 4)]
    return sonuc if okundu else None


def obd(pid):
    return [0x01, pid]


def did(kimlik):
    return [0x22, kimlik >> 8, kimlik & 0xFF]


TABLO = {"r60": tablo_yukle("r60"), "g20": tablo_yukle("g20")}


def T(kod, anahtar, baslik, birim, bicim, kimlik, carpan=1.0, deneme=0):
    """Ölçeği BMW tablosundan alınan canlı değer (kimlik: örn. 0x4807).
    carpan yalnızca birim çevirmek içindir (örn. hPa -> bar)."""
    kayit = TABLO[kod]["olcum"].get("%04X" % kimlik)
    if kayit is None:                       # tablo yoksa değer gösterilmez
        def cevir(d):
            raise IndexError
    else:
        _, _, tip, carp, ekle = kayit

        def cevir(d):
            return (ham_coz(tip, d) * carp + ekle) * carpan
    return P(anahtar, baslik, birim, bicim, did(kimlik), cevir, deneme)


# Araçta görülen hata kodlarının Türkçe karşılıkları (tablo Almanca)
HATA_TR = {
    ("r60", "2B67"): "Hava kütlesi akışı, uyumsuzluk",
    ("g20", "801C10"): "Uyandırma kaydı: “park, araç ağı uygun değil” "
                       "durumu istendi",
    ("g20", "801C16"): "“Park işlevleri, müşteri araçta değil” durumunda "
                       "hatalı uykuya geçiş",
}


ARACLAR = {
    "r60": {
        "ad": "Mini R60",
        "alt": "Countryman · N16 · MEV17.2",
        "not": "WiFi ağı: MHD_…  ·  eski mod",
        "baglanti": KwpBaglanti,
        "ecu": 0x12,
        "rolanti_alt": 600,
        "hizli": ["devir", "gerilim"],
        "su": "su",
        "hata_oku": r60_hata_oku,
        "hata_ornek": [("Motor beyni", "2B67", 0xA1)],
        # İlk on değer standart OBD isteğiyle okunur (araçta doğrulandı);
        # emme basıncı ve hava kütlesi BMW tablosundan, araçta denenmedi.
        "parametreler": [
            P("devir", "Devir", "d/dk", "%.0f", obd(0x0C),
              lambda d: u16(d) / 4),
            P("gerilim", "Gerilim", "V", "%.2f", obd(0x42),
              lambda d: u16(d) / 1000),
            P("su", "Su sıcaklığı", "°C", "%.0f", obd(0x05),
              lambda d: d[0] - 40, 20),
            P("lambda", "Lambda", "", "%.2f", obd(0x34),
              lambda d: u16(d) * 2 / 65536, 1.0),
            P("kisa", "Kısa yakıt düzeltme", "%", "%+.1f", obd(0x06),
              lambda d: (d[0] - 128) * 100 / 128),
            P("uzun", "Uzun yakıt düzeltme", "%", "%+.1f", obd(0x07),
              lambda d: (d[0] - 128) * 100 / 128),
            T("r60", "basinc", "Emme basıncı", "hPa", "%.0f", 0x4202,
              deneme=340),
            T("r60", "hava", "Hava kütlesi", "kg/sa", "%.0f", 0x5818,
              deneme=12),
            P("kelebek", "Gaz kelebeği", "%", "%.0f", obd(0x11),
              lambda d: d[0] * 100 / 255, 17),
            P("avans", "Ateşleme avansı", "°", "%.1f", obd(0x0E),
              lambda d: d[0] / 2 - 64, 5),
            P("emme", "Emme havası", "°C", "%.0f", obd(0x0F),
              lambda d: d[0] - 40, 18),
            P("sure", "Çalışma süresi", "sn", "%.0f", obd(0x1F), u16),
        ],
    },
    "g20": {
        "ad": "BMW G20",
        "alt": "3 Serisi · motor beyni (DME)",
        "not": "WiFi ağı: MHD ENET …  ·  ENET modu",
        "baglanti": HsfzBaglanti,
        "ecu": 0x12,
        "rolanti_alt": 550,
        "hizli": ["devir", "gerilim"],
        "su": "su",
        "hata_oku": g20_hata_oku,
        "hata_ornek": [("Ana geçit", "801C10", 0x4C),
                       ("Ana geçit", "801C16", 0x4C)],
        # Kimlik ve ölçekler BMW tablosundan (dme8ff_r); araçtaki kayıtla
        # karşılaştırılıp tutarlı bulundu.
        "parametreler": [
            T("g20", "devir", "Devir", "d/dk", "%.0f", 0x4807),
            T("g20", "gerilim", "Gerilim", "V", "%.2f", 0x59A7),
            T("g20", "su", "Su sıcaklığı", "°C", "%.0f", 0x4300, deneme=20),
            T("g20", "yag", "Yağ sıcaklığı", "°C", "%.0f", 0x4402, deneme=94),
            T("g20", "yagb", "Yağ basıncı", "bar", "%.2f", 0x586F, 0.001,
              deneme=2.5),
            T("g20", "lambda", "Lambda", "", "%.2f", 0x5889, deneme=1.0),
            T("g20", "hava", "Hava kütlesi", "kg/sa", "%.1f", 0x4ABC,
              deneme=10),
            T("g20", "ray", "Yakıt ray basıncı", "bar", "%.0f", 0x5658, 10,
              deneme=160),
            T("g20", "kelebek", "Gaz kelebeği", "%", "%.1f", 0x4600,
              deneme=2.6),
            T("g20", "pedal", "Gaz pedalı", "%", "%.0f", 0x480B),
            T("g20", "avans", "Ateşleme avansı", "°", "%.1f", 0x59FA,
              deneme=4),
            T("g20", "emme", "Emme havası", "°C", "%.0f", 0x580F, deneme=45),
        ],
    },
}


class Benzetim:
    """Deneme modu: 45 saniyelik bir senaryoyu tekrar eder."""

    def __init__(self):
        self.t0 = time.time()

    def deger(self, p):
        gecen = time.time() - self.t0
        t = gecen % 45
        su = 8 + gecen * 0.08
        devir, volt, sure, lam = 0, 12.2, 0, 1.0
        if 4 <= t < 7:                               # tutmayan marş
            devir, volt = 170 + random.uniform(-25, 25), 9.9
        elif 12 <= t < 13.2:                         # tutan marş
            devir, volt = 185 + random.uniform(-25, 25), 9.8
        elif 13.2 <= t < 38:                         # çalışıyor
            sure = t - 13.2
            devir = 720 + 500 * math.exp(-sure * 1.5) + random.uniform(-14, 14)
            if 9 < sure < 9.4 or 15 < sure < 15.3:   # tekleme
                devir -= 190
            volt = 11.7 if sure < 5 else min(13.9, 12.4 + (sure - 5) * 0.4)
            lam = 0.82 if 4 < sure < 7 else 1.0 + random.uniform(-.03, .03)
        time.sleep(0.01)
        return {"devir": devir, "gerilim": volt, "su": su, "sure": sure,
                "lambda": lam}.get(p.anahtar, p.deneme)


class Okuyucu(threading.Thread):
    """Arka planda araçla konuşur; sonuçları kuyruğa koyar."""

    def __init__(self, kod, deneme, kuyruk):
        super().__init__(daemon=True)
        self.kod, self.arac = kod, ARACLAR[kod]
        self.deneme, self.kuyruk = deneme, kuyruk
        self.dur = threading.Event()
        self.hata_iste = threading.Event()
        self.tum_iste = threading.Event()

    def _hatalar(self, bag):
        if self.deneme:
            return list(self.arac["hata_ornek"])
        return self.arac["hata_oku"](bag)

    def _tumu(self, bag):
        """Tablodaki bütün ölçümleri bir kez okur: [(kimlik, ad, değer,
        birim)]; yalnızca beynin cevap verdikleri döner."""
        olcum = TABLO[self.kod]["olcum"]
        sonuc = []
        for i, kim in enumerate(sorted(olcum)):
            if self.dur.is_set():
                break
            if i % 50 == 0:
                self.kuyruk.put(("bilgi", "Tüm değerler okunuyor: %d / %d"
                                 % (i, len(olcum))))
            ad, birim, tip, carp, ekle = olcum[kim]
            if self.deneme:
                sonuc.append((kim, ad, 0.0, birim))
                continue
            k = int(kim, 16)
            c = bag.sor(self.arac["ecu"], did(k), 0.5)
            if c and c[:3] == bytes([0x62, k >> 8, k & 0xFF]):
                try:
                    sonuc.append((kim, ad, ham_coz(tip, c[3:]) * carp + ekle,
                                  birim))
                except (IndexError, struct.error):
                    pass
        return sonuc

    def run(self):
        arac = self.arac
        ps = {p.anahtar: p for p in arac["parametreler"]}
        yavas = [k for k in ps if k not in arac["hizli"]]
        sutun = arac["hizli"] + yavas
        bag, sim = None, Benzetim() if self.deneme else None
        try:
            if not self.deneme:
                bag = arac["baglanti"]()
        except OSError as e:
            self.kuyruk.put(("koptu", "Bağlanılamadı: %s" % e))
            return
        os.makedirs(KAYIT_KLASORU, exist_ok=True)
        yol = os.path.join(KAYIT_KLASORU, "%s_%s%s.csv" % (
            self.kod, datetime.now().strftime("%Y%m%d_%H%M%S"),
            "_deneme" if self.deneme else ""))
        self.kuyruk.put(("baglandi", yol))
        son, tur, kopma = {}, 0, 0
        try:
            with open(yol, "w", encoding="utf-8") as csv:
                csv.write("saat;" + ";".join(sutun) + "\n")
                while not self.dur.is_set():
                    try:
                        if self.hata_iste.is_set():
                            self.hata_iste.clear()
                            self.kuyruk.put(("hatalar", self._hatalar(bag)))
                        if self.tum_iste.is_set():
                            self.tum_iste.clear()
                            self.kuyruk.put(("tum", self._tumu(bag)))
                            self.kuyruk.put(("bilgi", "Kaydediliyor: " + yol))
                        for k in arac["hizli"] + [yavas[tur % len(yavas)]]:
                            p = ps[k]
                            v = sim.deger(p) if sim else p.coz(
                                bag.sor(arac["ecu"], p.istek))
                            if v is not None:
                                son[k] = v
                        kopma = 0
                    except (ConnectionError, OSError):
                        # Adaptör bağlantıyı ara sıra kapatıyor: yeniden dene
                        kopma += 1
                        if kopma > 5:
                            raise
                        self.kuyruk.put(("bilgi", "Bağlantı koptu, yeniden "
                                         "bağlanılıyor…"))
                        bag.kapat()
                        time.sleep(1.0)
                        bag = arac["baglanti"]()
                        self.kuyruk.put(("bilgi", "Kaydediliyor: " + yol))
                        continue
                    tur += 1
                    csv.write(datetime.now().strftime("%H:%M:%S.%f")[:-3] + ";"
                              + ";".join("%.3f" % son[k] if k in son else ""
                                         for k in sutun) + "\n")
                    csv.flush()
                    self.kuyruk.put(("veri", time.time(), dict(son)))
        except (ConnectionError, OSError) as e:
            self.kuyruk.put(("koptu", "Bağlantı koptu: %s" % e))
        finally:
            if bag:
                bag.kapat()


# ====================================================================
# Arayüz
# ====================================================================
class Uygulama(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Araç Tarama")
        self.configure(bg=ZEMIN)
        self.geometry("1100x760")
        self.minsize(900, 640)
        self.kuyruk = queue.Queue()
        self.okuyucu = None
        self.kod = None
        self.deneme = tk.BooleanVar(value=False)
        self.govde = tk.Frame(self, bg=ZEMIN)
        self.govde.pack(fill="both", expand=True)
        stil = ttk.Style(self)
        stil.theme_use("clam")
        stil.configure("Treeview", background=KUTU, fieldbackground=KUTU,
                       foreground=YAZI, rowheight=26, borderwidth=0,
                       font=(FONT, 10))
        stil.configure("Treeview.Heading", background=CIZGI, foreground=SOLUK,
                       borderwidth=0, font=(FONT, 9))
        stil.map("Treeview", background=[("selected", CIZGI)])
        self.protocol("WM_DELETE_WINDOW", self._kapat)
        self._secim_ekrani()
        self.after(100, self._isle)

    def _temizle(self):
        if self.okuyucu:
            self.okuyucu.dur.set()
            self.okuyucu = None
        for w in self.govde.winfo_children():
            w.destroy()

    def _dugme(self, ust, yazi, komut, renk):
        return tk.Button(ust, text=yazi, command=komut, bg=renk, fg=YAZI,
                         activebackground=CIZGI, activeforeground=YAZI,
                         relief="flat", bd=0, padx=14, pady=6,
                         font=(FONT, 10), cursor="hand2")

    # ---------- araç seçimi ----------
    def _secim_ekrani(self):
        self._temizle()
        self.kod = None
        self.title("Araç Tarama")
        orta = tk.Frame(self.govde, bg=ZEMIN)
        orta.place(relx=0.5, rely=0.46, anchor="center")
        tk.Label(orta, text="Araç Tarama", bg=ZEMIN, fg=YAZI,
                 font=(FONT, 26, "bold")).pack()
        tk.Label(orta, text="Taramak istediğin aracı seç", bg=ZEMIN, fg=SOLUK,
                 font=(FONT, 12)).pack(pady=(4, 26))
        kartlar = tk.Frame(orta, bg=ZEMIN)
        kartlar.pack()
        en, boy = 440, 290
        self._resimler = []                 # Tk resimleri silinmesin diye
        for kod, arac in ARACLAR.items():
            k = tk.Canvas(kartlar, width=en, height=boy, bg=KUTU,
                          highlightthickness=0, cursor="hand2")
            k.pack(side="left", padx=12)
            resim = kart_resmi(kod, en, boy)
            if resim is not None:
                self._resimler.append(resim)
                k.create_image(0, 0, image=resim, anchor="nw")
            k.create_text(24, 22, text=arac["ad"], fill="#ffffff", anchor="nw",
                          font=(FONT, 22, "bold"))
            k.create_text(24, 72, text=arac["alt"], fill="#d4d9e0",
                          anchor="nw", font=(FONT, 10))
            k.create_text(24, boy - 20, text=arac["not"], fill="#d4d9e0",
                          anchor="sw", font=(FONT, 9))
            k.create_text(en - 24, boy - 20, text="Seç  →", fill="#ffffff",
                          anchor="se", font=(FONT, 11, "bold"))
            k.bind("<Button-1>", lambda e, kod=kod: self._panel_ekrani(kod))
        tk.Checkbutton(orta, text="Deneme modu (araçsız, sahte veriyle)",
                       variable=self.deneme, bg=ZEMIN, fg=SOLUK,
                       selectcolor=KUTU, activebackground=ZEMIN,
                       activeforeground=YAZI, font=(FONT, 10)).pack(pady=24)
        tk.Label(orta, text="Yalnızca okuma yapar: hata silmez, kodlama "
                 "yapmaz, beyne hiçbir şey yazmaz.", bg=ZEMIN, fg=SOLUK,
                 font=(FONT, 9)).pack()

    # ---------- panel ----------
    def _panel_ekrani(self, kod):
        self._temizle()
        self.kod, arac = kod, ARACLAR[kod]
        self.title("Araç Tarama · " + arac["ad"])
        self.gecmis, self.durum, self.olay = [], "bekliyor", {}
        g = self.govde

        ust = tk.Frame(g, bg=ZEMIN)
        ust.pack(fill="x", padx=16, pady=(14, 8))
        self._dugme(ust, "‹  Araç seç", self._secim_ekrani, KUTU).pack(
            side="left")
        tk.Label(ust, text=arac["ad"], bg=ZEMIN, fg=YAZI,
                 font=(FONT, 16, "bold")).pack(side="left", padx=(14, 0))
        self.durum_yazi = tk.Label(ust, text="●  Bağlı değil", bg=ZEMIN,
                                   fg=SOLUK, font=(FONT, 11))
        self.durum_yazi.pack(side="left", padx=16)
        self.baglan_dugme = self._dugme(ust, "Taramayı başlat", self._baglan,
                                        MAVI)
        self.baglan_dugme.pack(side="right")
        self._dugme(ust, "Hata kodlarını oku", self._hata_oku, KUTU).pack(
            side="right", padx=8)
        self._dugme(ust, "Tüm değerler", self._tum_oku, KUTU).pack(
            side="right")
        self._dugme(ust, "Kayıt klasörü", self._klasor_ac, KUTU).pack(
            side="right", padx=8)

        izgara = tk.Frame(g, bg=ZEMIN)
        izgara.pack(fill="x", padx=12)
        self.deger = {}
        for i, p in enumerate(arac["parametreler"]):
            k = tk.Frame(izgara, bg=KUTU)
            k.grid(row=i // 6, column=i % 6, padx=4, pady=4, sticky="nsew")
            izgara.columnconfigure(i % 6, weight=1, uniform="k")
            tk.Label(k, text=p.baslik, bg=KUTU, fg=SOLUK, font=(FONT, 9),
                     anchor="w").pack(fill="x", padx=12, pady=(10, 0))
            satir = tk.Frame(k, bg=KUTU)
            satir.pack(fill="x", padx=12, pady=(0, 10))
            d = tk.Label(satir, text="–", bg=KUTU, fg=YAZI,
                         font=(FONT, 22, "bold"))
            d.pack(side="left")
            tk.Label(satir, text=p.birim, bg=KUTU, fg=SOLUK,
                     font=(FONT, 10)).pack(side="left", padx=5, pady=(9, 0))
            self.deger[p.anahtar] = (d, p.bicim)

        self.grafik = tk.Canvas(g, bg=KUTU, highlightthickness=0, height=220)
        self.grafik.pack(fill="both", expand=True, padx=16, pady=(8, 8))

        alt = tk.Frame(g, bg=ZEMIN)
        alt.pack(fill="x", padx=16, pady=(0, 6))
        tk.Label(alt, text="Marş kayıtları", bg=ZEMIN, fg=YAZI,
                 font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 4))
        basliklar = [("saat", "Saat", 80), ("su", "Su °C", 70),
                     ("mars", "Marş süresi", 100), ("volt", "En düşük V", 95),
                     ("sonuc", "Sonuç", 150), ("not", "Çalışırken", 420)]
        self.liste = ttk.Treeview(alt, columns=[b[0] for b in basliklar],
                                  show="headings", height=5)
        for ad, yazi, en in basliklar:
            self.liste.heading(ad, text=yazi, anchor="w")
            self.liste.column(ad, width=en, anchor="w")
        self.liste.tag_configure("kotu", foreground=KIRMIZI)
        self.liste.tag_configure("iyi", foreground=YESIL)
        self.liste.pack(fill="x")

        self.bilgi = tk.Label(g, text="Kontağı aç, adaptörün WiFi ağına "
                              "bağlan (%s), sonra “Taramayı başlat”a bas."
                              % arac["not"].split("·")[0].strip(), bg=ZEMIN,
                              fg=SOLUK, font=(FONT, 9), anchor="w")
        self.bilgi.pack(fill="x", padx=16, pady=(2, 10))
        if self.deneme.get():
            self._baglan()

    # ---------- düğmeler ----------
    def _baglan(self):
        if self.okuyucu and self.okuyucu.is_alive():
            self.okuyucu.dur.set()
            self.okuyucu = None
            self._bagli_degil("Tarama durduruldu.")
            return
        self.durum_yazi.config(text="●  Bağlanıyor…", fg=SARI)
        self.okuyucu = Okuyucu(self.kod, self.deneme.get(), self.kuyruk)
        self.okuyucu.start()

    def _bagli_degil(self, mesaj):
        self.durum_yazi.config(text="●  Bağlı değil", fg=SOLUK)
        self.baglan_dugme.config(text="Taramayı başlat", bg=MAVI)
        self.bilgi.config(text=mesaj)

    def _hata_oku(self):
        if self.okuyucu and self.okuyucu.is_alive():
            self.okuyucu.hata_iste.set()
            self.bilgi.config(text="Hata hafızası okunuyor…")
        else:
            self.bilgi.config(text="Önce taramayı başlat.")

    def _tum_oku(self):
        if self.okuyucu and self.okuyucu.is_alive():
            self.okuyucu.tum_iste.set()
            self.bilgi.config(text="Tüm değerler okunuyor…")
        else:
            self.bilgi.config(text="Önce taramayı başlat.")

    def _tum_goster(self, satirlar):
        os.makedirs(KAYIT_KLASORU, exist_ok=True)
        yol = os.path.join(KAYIT_KLASORU, "%s_tum_%s.csv" % (
            self.kod, datetime.now().strftime("%Y%m%d_%H%M%S")))
        with open(yol, "w", encoding="utf-8-sig") as f:
            f.write("kimlik;ad;deger;birim\n")
            for kim, ad, deger, birim in satirlar:
                f.write("%s;%s;%.6g;%s\n" % (kim, ad.replace(";", ","), deger,
                                             birim))
        p = tk.Toplevel(self, bg=ZEMIN)
        p.title("Tüm değerler · " + ARACLAR[self.kod]["ad"])
        p.geometry("900x640")
        ust = tk.Frame(p, bg=ZEMIN)
        ust.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(ust, text="%d değer okundu" % len(satirlar), bg=ZEMIN,
                 fg=YAZI, font=(FONT, 13, "bold")).pack(side="left")
        tk.Label(ust, text="Ara:", bg=ZEMIN, fg=SOLUK,
                 font=(FONT, 10)).pack(side="left", padx=(24, 6))
        ara = tk.Entry(ust, bg=KUTU, fg=YAZI, insertbackground=YAZI,
                       relief="flat", font=(FONT, 11), width=30)
        ara.pack(side="left", ipady=4)
        tk.Label(p, text="Adlar BMW'nin teşhis tanımlarından geldiği için "
                 "Almanca. Anlık görüntüdür, canlı güncellenmez. Dosya: "
                 + yol, bg=ZEMIN, fg=SOLUK, font=(FONT, 9), anchor="w",
                 wraplength=860, justify="left").pack(fill="x", padx=16)
        cerceve = tk.Frame(p, bg=ZEMIN)
        cerceve.pack(fill="both", expand=True, padx=16, pady=12)
        liste = ttk.Treeview(cerceve, columns=("kim", "ad", "deger", "birim"),
                             show="headings")
        for ad, yazi, en in (("kim", "Kimlik", 70), ("ad", "Ad", 520),
                             ("deger", "Değer", 120), ("birim", "Birim", 90)):
            liste.heading(ad, text=yazi, anchor="w")
            liste.column(ad, width=en, anchor="w")
        kaydir = ttk.Scrollbar(cerceve, orient="vertical",
                               command=liste.yview)
        liste.configure(yscrollcommand=kaydir.set)
        kaydir.pack(side="right", fill="y")
        liste.pack(side="left", fill="both", expand=True)

        def doldur(*_):
            aranan = ara.get().lower()
            liste.delete(*liste.get_children())
            for kim, ad, deger, birim in satirlar:
                if aranan in (kim + " " + ad + " " + birim).lower():
                    liste.insert("", "end", values=(kim, ad, "%.6g" % deger,
                                                    birim))
        ara.bind("<KeyRelease>", doldur)
        doldur()
        self.bilgi.config(text="Tüm değerler okundu: " + yol)

    def _klasor_ac(self):
        os.makedirs(KAYIT_KLASORU, exist_ok=True)
        os.startfile(KAYIT_KLASORU)

    def _kapat(self):
        if self.okuyucu:
            self.okuyucu.dur.set()
        self.destroy()

    # ---------- veri akışı ----------
    def _isle(self):
        yeni = False
        try:
            while True:
                m = self.kuyruk.get_nowait()
                if self.kod is None or self.okuyucu is None:
                    continue                      # eski oturumdan kalan mesaj
                if m[0] == "baglandi":
                    self.durum_yazi.config(
                        text="●  Bağlı" + ("  (deneme)" if "_deneme" in m[1]
                                           else ""), fg=YESIL)
                    self.baglan_dugme.config(text="Taramayı durdur", bg=KUTU)
                    self.bilgi.config(text="Kaydediliyor: " + m[1])
                elif m[0] == "koptu":
                    self.okuyucu = None
                    self._bagli_degil(m[1])
                elif m[0] == "bilgi":
                    self.bilgi.config(text=m[1])
                elif m[0] == "hatalar":
                    self._hata_goster(m[1])
                elif m[0] == "tum":
                    self._tum_goster(m[1])
                elif m[0] == "veri":
                    self._veri(m[1], m[2])
                    yeni = True
        except queue.Empty:
            pass
        if yeni:
            self._ciz()
        self.after(100, self._isle)

    def _veri(self, t, v):
        for anahtar, (etiket, bicim) in self.deger.items():
            if anahtar in v:
                etiket.config(text=bicim % v[anahtar])
        devir, volt = v.get("devir", 0), v.get("gerilim")
        if volt is not None:
            self.deger["gerilim"][0].config(
                fg=KIRMIZI if volt < 10.5 else SARI if volt < 12 else YAZI)
        self.gecmis.append((t, devir, volt))
        while self.gecmis and self.gecmis[0][0] < t - 60:
            self.gecmis.pop(0)
        self._mars_izle(t, devir, volt, v)

    def _mars_izle(self, t, devir, volt, v):
        arac, o = ARACLAR[self.kod], self.olay
        if self.durum == "bekliyor" and devir > 0:
            self.durum = "mars"
            self.olay = {"t0": t, "saat": datetime.now().strftime("%H:%M:%S"),
                         "su": v.get(arac["su"]) if arac["su"] else None,
                         "volt": volt, "ust": None}
        elif self.durum == "mars":
            if volt is not None:
                o["volt"] = min(o["volt"] or volt, volt)
            if devir >= 500:
                o["ust"] = o["ust"] or t
                if t - o["ust"] >= 0.8:
                    self.durum = "calisiyor"
                    o.update(mars=o["ust"] - o["t0"], basla=o["ust"],
                             dusuk=None, dalma=0, altta=False)
                    o["satir"] = self._satir(o, "Çalıştı", "iyi", "çalışıyor…")
            else:
                o["ust"] = None
                if devir == 0:
                    self.durum = "bekliyor"
                    o["mars"] = t - o["t0"]
                    self._satir(o, "ÇALIŞMADI", "kotu", "")
        elif self.durum == "calisiyor":
            if devir == 0:
                self.durum = "bekliyor"
                self._satir_not(o, "%.0f sn çalıştı" % (t - o["basla"]))
            elif t - o["basla"] > 3:                # ilk 3 sn yüksek rölanti
                esik = arac["rolanti_alt"]
                o["dusuk"] = min(o["dusuk"] or devir, devir)
                if devir < esik and not o["altta"]:
                    o["dalma"] += 1
                o["altta"] = devir < esik
                self._satir_not(o, "%.0f sn" % (t - o["basla"]))

    def _satir(self, o, sonuc, etiket, notu):
        return self.liste.insert("", 0, tags=(etiket,), values=(
            o["saat"], "%.0f" % o["su"] if o["su"] is not None else "–",
            "%.1f sn" % o["mars"],
            "%.2f" % o["volt"] if o["volt"] is not None else "–",
            sonuc, notu))

    def _satir_not(self, o, sure):
        yazi = sure
        if o["dusuk"] is not None:
            yazi += "  ·  en düşük rölanti %.0f" % o["dusuk"]
            if o["dalma"]:
                yazi += "  ·  %d kez %d altına düştü" % (
                    o["dalma"], ARACLAR[self.kod]["rolanti_alt"])
        self.liste.set(o["satir"], "not", yazi)
        if o["dalma"]:
            self.liste.item(o["satir"], tags=())

    def _hata_goster(self, kayitlar):
        if kayitlar is None:
            self.bilgi.config(text="Hata hafızası okunamadı.")
            return
        tablo = TABLO[self.kod]["hata"]

        def aciklama(kaynak, kod):
            almanca = tablo.get(kaynak, {}).get(kod)
            turkce = HATA_TR.get((self.kod, kod))
            if turkce and almanca:
                return "%s\n(%s)" % (turkce, almanca)
            return turkce or almanca or "Açıklama tabloda yok"

        p = tk.Toplevel(self, bg=ZEMIN)
        p.title("Hata hafızası")
        p.geometry("680x%d" % (130 + 72 * max(1, len(kayitlar))))
        tk.Label(p, text="Hata hafızası: %d kayıt" % len(kayitlar), bg=ZEMIN,
                 fg=YAZI, font=(FONT, 13, "bold")).pack(anchor="w", padx=18,
                                                        pady=(16, 8))
        if not kayitlar:
            tk.Label(p, text="Kayıtlı hata yok.", bg=ZEMIN, fg=YESIL,
                     font=(FONT, 11)).pack(anchor="w", padx=18)
        for kaynak, kod, durum in kayitlar:
            s = tk.Frame(p, bg=KUTU)
            s.pack(fill="x", padx=18, pady=3)
            tk.Label(s, text=kod, bg=KUTU, fg=SARI, width=8,
                     font=(FONT, 13, "bold")).pack(side="left", padx=8, pady=8)
            tk.Label(s, text="%s  ·  durum %02X\n%s" % (
                kaynak, durum, aciklama(kaynak, kod)),
                bg=KUTU, fg=YAZI, font=(FONT, 10), wraplength=500,
                justify="left").pack(side="left", padx=4)
        self.bilgi.config(text="Hata hafızası okundu (silinmedi).")

    # ---------- grafik ----------
    def _ciz(self):
        g = self.grafik
        g.delete("all")
        en, boy = g.winfo_width(), g.winfo_height()
        sol, sag, ust, alt = 52, 52, 30, 24
        if en < 200 or boy < 100 or not self.gecmis:
            return
        son = self.gecmis[-1][0]
        tavan = max(2000, math.ceil(max(d for _, d, _ in self.gecmis) / 500)
                    * 500)
        g.create_text(sol, 14, text="Devir (d/dk)", fill=MAVI, anchor="w",
                      font=(FONT, 9, "bold"))
        g.create_text(en - sag, 14, text="Gerilim (V)", fill=SARI, anchor="e",
                      font=(FONT, 9, "bold"))
        for i in range(5):
            y = ust + (boy - ust - alt) * i / 4
            g.create_line(sol, y, en - sag, y, fill=CIZGI)
            g.create_text(sol - 8, y, text="%d" % (tavan * (4 - i) / 4),
                          fill=SOLUK, anchor="e", font=(FONT, 8))
            g.create_text(en - sag + 8, y, text="%.0f" % (16 - 2 * i),
                          fill=SOLUK, anchor="w", font=(FONT, 8))
        for sn in (0, 15, 30, 45, 60):
            x = en - sag - (en - sol - sag) * sn / 60
            g.create_text(x, boy - 10, text="şimdi" if sn == 0 else "-%d sn"
                          % sn, fill=SOLUK, font=(FONT, 8))

        def nokta(t, deger, en_az, en_cok):
            x = en - sag - (en - sol - sag) * (son - t) / 60
            oran = min(1, max(0, (deger - en_az) / (en_cok - en_az)))
            return x, boy - alt - (boy - ust - alt) * oran

        devir = [c for t, d, _ in self.gecmis for c in nokta(t, d, 0, tavan)]
        volt = [c for t, _, v in self.gecmis if v is not None
                for c in nokta(t, v, 8, 16)]
        if len(volt) >= 4:
            g.create_line(*volt, fill=SARI, width=2)
        if len(devir) >= 4:
            g.create_line(*devir, fill=MAVI, width=2)


if __name__ == "__main__":
    Uygulama().mainloop()
