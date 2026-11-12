# Araç Tarama

Mini R60 ve BMW G20 için canlı veri ve hata hafızası paneli. MHD WiFi adaptörü üzerinden çalışır.

Yalnızca okuma yapar: canlı değerleri ve hata hafızasını gösterir. Hata silmez, kodlama yapmaz, araç beyinlerine hiçbir şey yazmaz.

## Çalıştırma

```
python arac_tarama.py
```

Windows'ta `Araç Tarama.bat` dosyasına çift tıklamak da yeterlidir. Araç yokken denemek için açılış ekranında "Deneme modu" kutusunu işaretleyin.

Her bağlantının kaydı `kayitlar` klasörüne ayrı bir CSV dosyası olarak yazılır. Bu klasör depoya girmez.

## İçindekiler

| Dosya | Ne işe yarar |
|---|---|
| `arac_tarama.py` | Programın tamamı (Python, tkinter) |
| `veri/g20.json`, `veri/r60.json` | Ölçüm ve hata kodu tabloları |
| `veri/bmw.SHA256SUMS` | Kaynak veritabanının sağlama değerleri |
| `resimler/` | Araç kartlarının arka plan resimleri |

Kaynak veritabanı (`veri/bmw.sqlite.gz`, 129 MB) GitHub'ın dosya boyutu sınırını aştığı için depoda yoktur.

## Kaynaklar

- **bmw-unified-db v1.2.0**: `veri` klasöründeki ölçüm ve hata tabloları bu veritabanından çıkarılmıştır (`dme8ff_r`, `zgw_sp18`, `bdc_g05`, `mv1722` tanımları). Tablolardaki Almanca ölçüm adları ve hata açıklamaları BMW'nin teşhis tanımlarına aittir.
- `resimler` klasöründeki araç görsellerinin hakları sahiplerine aittir.

Bu depo herkese açılmadan önce yukarıdaki veri ve görsellerin paylaşım hakları ayrıca değerlendirilmelidir.

BMW, MINI ve MHD ilgili sahiplerinin markalarıdır. Bu proje bu firmalarla bağlantılı değildir.
