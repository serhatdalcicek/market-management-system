"""
Market Takip
============

Tedarikçi / alış / fotoğraf takibi yapan masaüstü uygulaması.

Mimari notlar
-------------
- Tüm kalıcı veri JSON (`market_data/data.json`) + dosya sistemi (`market_data/photos`)
  üzerinde tutulur. Veritabanı sunucusu yoktur, tek kullanıcı / tek makine (veya
  OneDrive gibi senkronize edilen tek bir klasör) için tasarlanmıştır.
- `DataStore` sınıfı tüm okuma/yazma/yedekleme mantığını kapsar ve UI'dan
  bağımsızdır (QMessageBox çağırmaz). UI katmanı hataları yakalayıp kullanıcıya
  gösterir. Bu ayrım ileride yeni özellik eklerken (stok, dashboard, rapor vb.)
  veri katmanına dokunmadan üstüne inşa etmeyi kolaylaştırır.
- Silme işlemlerinde sıra HER ZAMAN: önce JSON güncellenir ve diske yazılır,
  sonra fiziksel dosyalar silinir. Fiziksel silme başarısız olsa bile JSON
  zaten güncel olduğu için o dosya/klasör bir sonraki açılışta "yetim" olarak
  tespit edilip otomatik temizlenir (bkz. cleanup_orphan_photo_folders).
- Üç katmanlı yedekleme:
    1) data.json.bak         -> her save'de anlık yedek (en hızlı kurtarma)
    2) backups/auto/*.json   -> son N kayıt, JSON-only rotasyon
    3) backups/manual/*.zip  -> "Yedek Oluştur" ile alınan, fotoğraflar dahil
                                 tam yedek (Yedekten Geri Yükle bunları kullanır)
- Tüm beklenmeyen hatalar `market_data/logs/app.log` içine loglanır, kullanıcıya
  ham traceback gösterilmez.

PyInstaller ile paketleme
--------------------------
    pyinstaller --onefile --noconsole --name MarketTakip --icon=app.ico main.py

`.exe` haline geldiğinde veri klasörü, PyInstaller'ın geçici çıkarma dizini
yerine (`sys._MEIPASS`) doğrudan exe'nin yanında oluşturulur (bkz. get_app_dir).
Bu sayede uygulama güncellense/yeniden paketlense bile kullanıcı verisi kalıcı
kalır.
"""

import sys
import json
import shutil
import sqlite3
import zipfile
import os
import stat
import time
import logging
from logging.handlers import RotatingFileHandler

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QSplitter,
    QHeaderView,
)


# ============================================================
# UYGULAMA BİLGİSİ
# ============================================================

APP_NAME = "Market Takip"
APP_VERSION = "2.0.0"


# ============================================================
# UYGULAMA / DOSYA YAPISI
# ============================================================

def get_app_dir() -> Path:
    """
    PyInstaller ile paketlenmiş .exe olarak çalışırken veri klasörünün
    exe'nin yanında oluşmasını garanti eder. Geliştirme ortamında ise
    main.py'nin bulunduğu klasördür.
    """

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()

DATA_DIR = APP_DIR / "market_data"
DATA_FILE = DATA_DIR / "data.json"
PHOTOS_DIR = DATA_DIR / "photos"
TRASH_DIR = DATA_DIR / ".trash"
BACKUPS_DIR = DATA_DIR / "backups"
AUTO_BACKUPS_DIR = BACKUPS_DIR / "auto"
MANUAL_BACKUPS_DIR = BACKUPS_DIR / "manual"
LOGS_DIR = DATA_DIR / "logs"

OLD_DATA_DIR = APP_DIR / "data"
OLD_DB_PATH = OLD_DATA_DIR / "market.db"

REQUIRED_DIRS = (
    DATA_DIR,
    PHOTOS_DIR,
    TRASH_DIR,
    BACKUPS_DIR,
    AUTO_BACKUPS_DIR,
    MANUAL_BACKUPS_DIR,
    LOGS_DIR,
)


# ============================================================
# GENEL AYARLAR
# ============================================================

PHOTO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
}

MAX_AUTO_BACKUPS = 10


# ============================================================
# LOGLAMA
# ============================================================

log = logging.getLogger("market_takip")


def setup_logging():
    log.setLevel(logging.DEBUG)

    if log.handlers:
        return log

    try:
        handler = RotatingFileHandler(
            LOGS_DIR / "app.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )

        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s"
            )
        )

        log.addHandler(handler)

    except Exception:
        # Log dosyası bile açılamıyorsa (izin sorunu vb.) sessizce
        # konsola düş; uygulamanın açılmasını engellemesin.
        console = logging.StreamHandler()
        log.addHandler(console)

    return log


def bootstrap():
    """
    Klasörleri ve loglamayı hazırlar. Bu adım QApplication oluşturulduktan
    hemen sonra çağrılır ki bir hata olursa kullanıcıya grafik bir mesaj
    kutusu ile bildirilebilsin (konsolsuz .exe'de tek yol budur).
    """

    for directory in REQUIRED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)

    setup_logging()

    log.info("=" * 60)
    log.info("%s v%s başlatıldı. APP_DIR=%s", APP_NAME, APP_VERSION, APP_DIR)


# ============================================================
# HATA TÜRLERİ
# ============================================================

class AppError(Exception):
    """
    Kullanıcıya gösterilmesi güvenli, beklenen/işlenmiş hata.
    `user_message` ekrana basılır, `technical` sadece log dosyasına yazılır.
    """

    def __init__(self, user_message, technical=None):
        super().__init__(user_message)
        self.user_message = user_message
        self.technical = technical or user_message


class DataError(AppError):
    pass


class BackupError(AppError):
    pass


class PhotoError(AppError):
    pass


def report_exception(parent, title, exc, fallback_message=None):
    """
    Bir hatayı log dosyasına tam detayıyla yazar, kullanıcıya ise sade ve
    anlaşılır bir mesaj kutusu gösterir. Uygulamanın hiçbir yerinde ham
    Python traceback'i kullanıcıya gösterilmemelidir; bu fonksiyon tek
    giriş noktasıdır.
    """

    log.exception(title)

    if isinstance(exc, AppError):
        message = exc.user_message
    else:
        message = fallback_message or "Beklenmeyen bir hata oluştu."

    QMessageBox.critical(
        parent,
        title,
        (
            f"{message}\n\n"
            "Sorun devam ederse aşağıdaki log dosyasını "
            "geliştiriciyle paylaşabilirsiniz:\n"
            f"{LOGS_DIR / 'app.log'}"
        ),
    )


def install_excepthook():
    """
    Uygulamanın herhangi bir yerinde yakalanmamış bir istisna oluşursa
    (programcının öngörmediği bir durum), uygulamanın sessizce çökmesi
    yerine loglanıp kullanıcıya bilgi verilmesini sağlar.
    """

    def hook(exc_type, exc_value, exc_tb):
        log.critical(
            "Yakalanmamış hata",
            exc_info=(exc_type, exc_value, exc_tb),
        )

        try:
            QMessageBox.critical(
                None,
                "Beklenmeyen Hata",
                (
                    "Uygulamada beklenmeyen bir hata oluştu ve işlem "
                    "tamamlanamamış olabilir.\n\n"
                    f"{exc_type.__name__}: {exc_value}\n\n"
                    "Detaylar log dosyasına kaydedildi:\n"
                    f"{LOGS_DIR / 'app.log'}"
                ),
            )
        except Exception:
            pass

    sys.excepthook = hook


# ============================================================
# VERİ YAPISI YARDIMCILARI
# ============================================================

def empty_data():
    return {
        "version": 2,
        "next_supplier_id": 1,
        "next_purchase_id": 1,
        "suppliers": [],
        "purchases": [],
    }


def normalize_data(data):
    """
    JSON'dan gelen veriyi güvenli ve beklenen yapıya getirir.
    Eski veya eksik JSON dosyaları uygulamayı bozmaz.
    """

    if not isinstance(data, dict):
        data = empty_data()

    data.setdefault("version", 2)
    data.setdefault("next_supplier_id", 1)
    data.setdefault("next_purchase_id", 1)
    data.setdefault("suppliers", [])
    data.setdefault("purchases", [])

    if not isinstance(data["suppliers"], list):
        data["suppliers"] = []

    if not isinstance(data["purchases"], list):
        data["purchases"] = []

    # --------------------------------------------------------
    # Supplier normalize
    # --------------------------------------------------------

    normalized_suppliers = []

    for supplier in data["suppliers"]:
        if not isinstance(supplier, dict):
            continue

        try:
            supplier_id = int(supplier.get("id", 0))
        except (TypeError, ValueError):
            continue

        if supplier_id <= 0:
            continue

        normalized_suppliers.append({
            "id": supplier_id,
            "name": str(supplier.get("name") or "").strip(),
            "phone": str(supplier.get("phone") or "").strip(),
            "address": str(supplier.get("address") or "").strip(),
            "notes": str(supplier.get("notes") or "").strip(),
            "created_at": supplier.get(
                "created_at", datetime.now().isoformat()
            ),
            **(
                {"updated_at": supplier["updated_at"]}
                if supplier.get("updated_at")
                else {}
            ),
        })

    data["suppliers"] = normalized_suppliers

    # --------------------------------------------------------
    # Purchase normalize
    # --------------------------------------------------------

    normalized_purchases = []

    for purchase in data["purchases"]:
        if not isinstance(purchase, dict):
            continue

        try:
            purchase_id = int(purchase.get("id", 0))
            supplier_id = int(purchase.get("supplier_id", 0))
        except (TypeError, ValueError):
            continue

        if purchase_id <= 0 or supplier_id <= 0:
            continue

        items = []
        raw_items = purchase.get("items", [])

        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue

                try:
                    quantity = float(item.get("quantity", 0) or 0)
                except (TypeError, ValueError):
                    quantity = 0

                try:
                    price = float(item.get("price", 0) or 0)
                except (TypeError, ValueError):
                    price = 0

                items.append({
                    "product_name": str(item.get("product_name") or "").strip(),
                    "quantity": quantity,
                    "unit": str(item.get("unit") or "").strip(),
                    "price": price,
                })

        photos = []
        raw_photos = purchase.get("photos", [])

        if isinstance(raw_photos, list):
            for photo in raw_photos:
                if not photo:
                    continue

                photo = str(photo).strip()

                if photo and photo not in photos:
                    photos.append(photo)

        try:
            total = float(purchase.get("total", 0) or 0)
        except (TypeError, ValueError):
            total = 0

        normalized_purchases.append({
            "id": purchase_id,
            "supplier_id": supplier_id,
            "date": str(
                purchase.get("date", datetime.now().strftime("%Y-%m-%d"))
            ),
            "total": total,
            "description": str(purchase.get("description") or "").strip(),
            "items": items,
            "photos": photos,
            "created_at": purchase.get(
                "created_at", datetime.now().isoformat()
            ),
            **(
                {"updated_at": purchase["updated_at"]}
                if purchase.get("updated_at")
                else {}
            ),
        })

    data["purchases"] = normalized_purchases

    # --------------------------------------------------------
    # Next ID'leri yeniden garanti altına al (çakışmayı önler)
    # --------------------------------------------------------

    supplier_ids = [int(x["id"]) for x in data["suppliers"]]
    purchase_ids = [int(x["id"]) for x in data["purchases"]]

    max_supplier_id = max(supplier_ids, default=0)
    max_purchase_id = max(purchase_ids, default=0)

    try:
        next_supplier = int(data.get("next_supplier_id", 1))
    except (TypeError, ValueError):
        next_supplier = 1

    try:
        next_purchase = int(data.get("next_purchase_id", 1))
    except (TypeError, ValueError):
        next_purchase = 1

    data["next_supplier_id"] = max(next_supplier, max_supplier_id + 1)
    data["next_purchase_id"] = max(next_purchase, max_purchase_id + 1)

    return data


def get_next_id(data, key):
    value = int(data.get(key, 1))
    data[key] = value + 1
    return value


# ============================================================
# PATH HELPERS
# ============================================================

def safe_relative_path(path):
    """JSON'da tutulan relative path'i gerçek dosya yoluna çevirir."""

    if not path:
        return None

    raw = Path(str(path))
    candidate = raw if raw.is_absolute() else APP_DIR / raw

    try:
        return candidate.resolve()
    except Exception:
        return candidate


def is_inside_directory(path, directory):
    """Bir path'in belirli klasörün içinde olup olmadığını kontrol eder."""

    try:
        path = Path(path).resolve()
        directory = Path(directory).resolve()
        return path == directory or directory in path.parents
    except Exception:
        return False


def relative_app_path(path):
    path = Path(path)

    try:
        return str(path.resolve().relative_to(APP_DIR.resolve()))
    except Exception:
        return str(path)


# ============================================================
# DOSYA SİLME / CLEANUP (Windows/OneDrive kilitlerine dayanıklı)
# ============================================================

def make_writable(path):
    """Windows read-only dosyalarda silme problemini azaltır."""

    try:
        os.chmod(str(path), stat.S_IWRITE)
    except Exception:
        pass


def remove_file_safely(path, retries=3):
    path = Path(path)

    if not path.exists():
        return True

    for attempt in range(retries):
        try:
            make_writable(path)
            path.unlink()
            return True

        except FileNotFoundError:
            return True

        except PermissionError:
            time.sleep(0.3 * (attempt + 1))

        except OSError:
            time.sleep(0.3 * (attempt + 1))

    log.warning("Dosya silinemedi (kilitli olabilir): %s", path)
    return False


def remove_directory_safely(path, retries=3):
    """
    Windows / OneDrive gibi ortamlarda klasör silmeyi mümkün olduğunca
    toleranslı yapar. Başarısız olursa False döner (uygulamayı bozmaz).
    """

    path = Path(path)

    if not path.exists():
        return True

    for attempt in range(retries):
        try:
            def on_error(func, failed_path, exc_info):
                make_writable(failed_path)
                try:
                    func(failed_path)
                except Exception:
                    pass

            shutil.rmtree(path, onerror=on_error)

            if not path.exists():
                return True

        except Exception:
            pass

        time.sleep(0.5 * (attempt + 1))

    ok = not path.exists()

    if not ok:
        log.warning("Klasör silinemedi (kilitli olabilir): %s", path)

    return ok


def move_to_trash(path):
    """
    Fotoğraf/klasörü direkt silmek yerine mümkünse `.trash` altına taşır.
    Bu, özellikle OneDrive gibi senkronizasyon yapan klasörlerde daha
    güvenlidir (dosya handle'ı henüz serbest bırakılmamış olabilir).

    Taşıma da başarısız olursa doğrudan silmeyi dener; o da başarısız
    olursa False döner ama bu DURUMU BOZMAZ: dosya/klasör JSON'da zaten
    referanssız kaldığı için bir sonraki açılışta yetim taraması onu
    tekrar yakalayıp tekrar dener.
    """

    path = Path(path)

    if not path.exists():
        return True

    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = TRASH_DIR / f"{path.name}_{timestamp}"

        shutil.move(str(path), str(target))
        return True

    except Exception:
        log.warning(
            "move_to_trash başarısız, doğrudan silme deneniyor: %s",
            path,
            exc_info=True,
        )

        if path.is_dir():
            return remove_directory_safely(path, retries=1)

        return remove_file_safely(path, retries=1)


def cleanup_trash():
    """
    `.trash` altındaki eski öğeleri temizlemeye çalışır. Başarısız olan
    öğeler orada kalır ve bir sonraki açılışta tekrar denenir.
    """

    if not TRASH_DIR.exists():
        return

    try:
        for item in TRASH_DIR.iterdir():
            if item.is_dir():
                remove_directory_safely(item, retries=1)
            else:
                remove_file_safely(item, retries=1)
    except Exception:
        log.warning("cleanup_trash sırasında hata.", exc_info=True)


def cleanup_orphan_photos(data):
    """
    market_data/photos altında JSON'da artık karşılığı olmayan
    supplier_*/purchase_* klasörlerini ve referanssız fotoğraf dosyalarını
    `.trash`'e taşır.

    Bu fonksiyon, "önce JSON güncelle sonra fiziksel dosya sil" akışında
    fiziksel silme adımı (kilit, OneDrive senkronu vb. yüzünden) başarısız
    olduğunda ortaya çıkan yetim klasör/dosya sorununu kalıcı olarak çözer:
    her açılışta otomatik çalıştığı için silme er ya da geç tamamlanır,
    manuel müdahale gerekmez.
    """

    if not PHOTOS_DIR.exists():
        return

    valid_supplier_ids = {int(s["id"]) for s in data.get("suppliers", [])}

    purchases_by_supplier = {}
    referenced_files_by_purchase = {}

    for purchase in data.get("purchases", []):
        supplier_id = int(purchase["supplier_id"])
        purchase_id = int(purchase["id"])

        purchases_by_supplier.setdefault(supplier_id, set()).add(purchase_id)

        referenced_names = set()
        for photo in purchase.get("photos", []):
            real = safe_relative_path(photo)
            if real:
                referenced_names.add(real.name)

        referenced_files_by_purchase[(supplier_id, purchase_id)] = referenced_names

    try:
        supplier_dirs = list(PHOTOS_DIR.iterdir())
    except Exception:
        log.warning("PHOTOS_DIR taranamadı.", exc_info=True)
        return

    for supplier_dir in supplier_dirs:
        if not supplier_dir.is_dir() or not supplier_dir.name.startswith("supplier_"):
            continue

        try:
            supplier_id = int(supplier_dir.name.split("_", 1)[1])
        except (IndexError, ValueError):
            supplier_id = None

        if supplier_id not in valid_supplier_ids:
            log.info("Yetim tedarikçi fotoğraf klasörü temizleniyor: %s", supplier_dir)
            move_to_trash(supplier_dir)
            continue

        valid_purchase_ids = purchases_by_supplier.get(supplier_id, set())

        try:
            purchase_dirs = list(supplier_dir.iterdir())
        except Exception:
            continue

        for purchase_dir in purchase_dirs:
            if not purchase_dir.is_dir() or not purchase_dir.name.startswith("purchase_"):
                continue

            try:
                purchase_id = int(purchase_dir.name.split("_", 1)[1])
            except (IndexError, ValueError):
                purchase_id = None

            if purchase_id not in valid_purchase_ids:
                log.info("Yetim alış fotoğraf klasörü temizleniyor: %s", purchase_dir)
                move_to_trash(purchase_dir)
                continue

            # Klasör geçerli: içindeki, JSON'da artık referanslı olmayan
            # tek tek dosyaları da temizle (örn. kopyalandı ama JSON
            # kaydı bir önceki oturumda başarısız olduysa).
            referenced_names = referenced_files_by_purchase.get(
                (supplier_id, purchase_id), set()
            )

            try:
                files = list(purchase_dir.iterdir())
            except Exception:
                continue

            for file_path in files:
                if file_path.is_file() and file_path.name not in referenced_names:
                    log.info("Yetim fotoğraf dosyası temizleniyor: %s", file_path)
                    move_to_trash(file_path)


# ============================================================
# VERİ KATMANI: DataStore
# ============================================================

class DataStore:
    """
    Tüm JSON okuma/yazma/yedekleme mantığını kapsar. UI'dan bağımsızdır;
    hata durumlarında QMessageBox göstermek yerine AppError alt sınıflarını
    fırlatır, UI katmanı bunları yakalayıp kullanıcıya gösterir.
    """

    def __init__(self):
        self.last_migration_error = None

    # --------------------------------------------------------
    # LOAD
    # --------------------------------------------------------

    def load(self):
        try:
            self._migrate_old_database()
        except Exception as exc:
            log.exception("Eski SQLite verisi aktarılırken hata oluştu.")
            self.last_migration_error = exc

        if not DATA_FILE.exists():
            data = empty_data()
            self.save(data)
            return data

        try:
            raw = DATA_FILE.read_text(encoding="utf-8")
            return normalize_data(json.loads(raw))

        except Exception as exc:
            log.warning("data.json okunamadı (%s), .bak deneniyor.", exc)

            recovered = self._try_recover_from_bak()
            if recovered is not None:
                return recovered

            recovered = self._try_recover_from_auto_backup()
            if recovered is not None:
                return recovered

            log.error("Hiçbir yedekten kurtarma başarılı olmadı.")

            raise DataError(
                "Veri dosyası okunamadı ve otomatik yedeklerden de "
                "kurtarılamadı. Uygulama boş bir veri seti ile açılacak.\n\n"
                "Lütfen market_data/backups klasöründeki yedekleri kontrol edin.",
                technical=str(exc),
            )

    def _try_recover_from_bak(self):
        backup_file = DATA_FILE.with_name(DATA_FILE.name + ".bak")

        if not backup_file.exists():
            return None

        try:
            data = normalize_data(json.loads(backup_file.read_text(encoding="utf-8")))
            self.save(data)
            log.info(".bak dosyasından başarıyla kurtarıldı.")
            return data
        except Exception:
            log.exception(".bak dosyasından kurtarma başarısız.")
            return None

    def _try_recover_from_auto_backup(self):
        if not AUTO_BACKUPS_DIR.exists():
            return None

        candidates = sorted(
            AUTO_BACKUPS_DIR.glob("data_*.json"), reverse=True
        )

        for candidate in candidates:
            try:
                data = normalize_data(
                    json.loads(candidate.read_text(encoding="utf-8"))
                )
                self.save(data)
                log.info("Otomatik yedekten kurtarıldı: %s", candidate.name)
                return data
            except Exception:
                continue

        return None

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    def save(self, data):
        """
        JSON'u doğrudan yazmak yerine önce temporary dosyaya yazar, sonra
        atomik olarak yerine koyar. Böylece uygulama yazma sırasında
        kapanırsa data.json'ın bozulma ihtimali en aza iner. Her başarılı
        yazmadan sonra `.bak` güncellenir ve rotasyonlu bir otomatik yedek
        alınır.
        """

        data = normalize_data(data)

        temp_file = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        backup_file = DATA_FILE.with_name(DATA_FILE.name + ".bak")

        content = json.dumps(data, ensure_ascii=False, indent=2)

        try:
            temp_file.write_text(content, encoding="utf-8")

            if DATA_FILE.exists():
                try:
                    shutil.copy2(DATA_FILE, backup_file)
                except Exception:
                    log.warning(".bak güncellenemedi.", exc_info=True)

            temp_file.replace(DATA_FILE)

        except Exception as exc:
            log.exception("data.json yazılamadı.")
            raise DataError(
                "Veri dosyasına yazılamadı; bu değişiklik kaydedilmedi.\n"
                "Diskte yer olduğundan ve dosyanın başka bir uygulama "
                "tarafından kilitlenmediğinden emin olun.",
                technical=str(exc),
            )

        self._rotate_auto_backup(content)

        return data

    def _rotate_auto_backup(self, content):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = AUTO_BACKUPS_DIR / f"data_{timestamp}.json"
            target.write_text(content, encoding="utf-8")

            existing = sorted(AUTO_BACKUPS_DIR.glob("data_*.json"))
            excess = len(existing) - MAX_AUTO_BACKUPS

            for old_file in existing[: max(excess, 0)]:
                remove_file_safely(old_file, retries=1)

        except Exception:
            # Otomatik yedek rotasyonu asıl kaydı engellemez.
            log.warning("Otomatik yedek rotasyonu başarısız.", exc_info=True)

    # --------------------------------------------------------
    # MANUEL YEDEK (ZIP, fotoğraflar dahil)
    # --------------------------------------------------------

    def create_backup(self):
        """
        data.json + tüm fotoğrafları içeren tam bir ZIP yedeği oluşturur ve
        `backups/manual/` altına timestamp'li olarak kaydeder. Dönen path,
        kullanıcı isterse başka bir konuma da kopyalanabilir.
        """

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        target = MANUAL_BACKUPS_DIR / f"MarketBackup_{timestamp}.zip"

        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                if DATA_FILE.exists():
                    archive.write(DATA_FILE, DATA_FILE.relative_to(APP_DIR))

                if PHOTOS_DIR.exists():
                    for file_path in PHOTOS_DIR.rglob("*"):
                        if file_path.is_file():
                            archive.write(
                                file_path, file_path.relative_to(APP_DIR)
                            )

            return target

        except Exception as exc:
            log.exception("Yedek oluşturulamadı.")
            remove_file_safely(target, retries=1)
            raise BackupError(
                "Yedek dosyası oluşturulamadı.", technical=str(exc)
            )

    def list_manual_backups(self):
        if not MANUAL_BACKUPS_DIR.exists():
            return []

        return sorted(
            MANUAL_BACKUPS_DIR.glob("*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    # --------------------------------------------------------
    # RESTORE
    # --------------------------------------------------------

    def restore_from_zip(self, zip_path):
        """
        Bir ZIP yedeğinden mevcut verinin ve fotoğrafların üzerine geri
        yükleme yapar. Geri yüklemeden önce mevcut durumun bir güvenlik
        kopyası alınır ki restore işlemi de geri alınabilir olsun.
        """

        zip_path = Path(zip_path)

        if not zip_path.exists():
            raise BackupError("Seçilen yedek dosyası bulunamadı.")

        try:
            self.create_backup()
        except BackupError:
            log.warning(
                "Restore öncesi güvenlik yedeği alınamadı, yine de devam ediliyor."
            )

        extract_dir = DATA_DIR / f".restore_tmp_{datetime.now():%Y%m%d_%H%M%S}"

        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                names = archive.namelist()

                if not any(name.replace("\\", "/").endswith("market_data/data.json") for name in names):
                    raise BackupError(
                        "Bu dosya geçerli bir Market Takip yedeği gibi görünmüyor."
                    )

                archive.extractall(extract_dir)

            extracted_data_file = extract_dir / "market_data" / "data.json"

            if not extracted_data_file.exists():
                raise BackupError("Yedek içinde data.json bulunamadı.")

            data = normalize_data(
                json.loads(extracted_data_file.read_text(encoding="utf-8"))
            )

            extracted_photos = extract_dir / "market_data" / "photos"

            if PHOTOS_DIR.exists():
                move_to_trash(PHOTOS_DIR)

            PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

            if extracted_photos.exists():
                for item in extracted_photos.iterdir():
                    shutil.move(str(item), str(PHOTOS_DIR / item.name))

            data = self.save(data)
            return data

        except BackupError:
            raise

        except Exception as exc:
            log.exception("Yedekten geri yükleme başarısız.")
            raise BackupError(
                "Yedekten geri yükleme sırasında bir hata oluştu. "
                "Mevcut verileriniz değiştirilmemiş olabilir.",
                technical=str(exc),
            )

        finally:
            if extract_dir.exists():
                remove_directory_safely(extract_dir, retries=1)

    # --------------------------------------------------------
    # SQLITE -> JSON MIGRATION
    # --------------------------------------------------------

    def _migrate_old_database(self):
        """Eski SQLAlchemy/SQLite sisteminden yeni JSON sistemine tek seferlik migration."""

        if DATA_FILE.exists():
            return

        if not OLD_DB_PATH.exists():
            return

        connection = None

        try:
            connection = sqlite3.connect(str(OLD_DB_PATH))
            connection.row_factory = sqlite3.Row
            cursor = connection.cursor()

            tables = {
                row["name"]
                for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            if "suppliers" not in tables or "purchases" not in tables:
                return

            data = empty_data()

            suppliers = cursor.execute(
                """
                SELECT id, name, phone, address, notes, created_at
                FROM suppliers ORDER BY id
                """
            ).fetchall()

            for row in suppliers:
                supplier_id = int(row["id"])

                data["suppliers"].append({
                    "id": supplier_id,
                    "name": row["name"] or "",
                    "phone": row["phone"] or "",
                    "address": row["address"] or "",
                    "notes": row["notes"] or "",
                    "created_at": row["created_at"] or datetime.now().isoformat(),
                })

            purchases = cursor.execute(
                """
                SELECT id, supplier_id, purchase_date, total_amount,
                       description, created_at
                FROM purchases ORDER BY id
                """
            ).fetchall()

            for purchase_row in purchases:
                purchase_id = int(purchase_row["id"])
                supplier_id = int(purchase_row["supplier_id"])

                items = []

                if "purchase_items" in tables:
                    item_rows = cursor.execute(
                        """
                        SELECT product_name, quantity, unit, price
                        FROM purchase_items
                        WHERE purchase_id = ? ORDER BY id
                        """,
                        (purchase_id,),
                    ).fetchall()

                    for item in item_rows:
                        items.append({
                            "product_name": item["product_name"] or "",
                            "quantity": float(item["quantity"] or 0),
                            "unit": item["unit"] or "",
                            "price": float(item["price"] or 0),
                        })

                photos = []

                if "purchase_photos" in tables:
                    photo_rows = cursor.execute(
                        """
                        SELECT file_path FROM purchase_photos
                        WHERE purchase_id = ? ORDER BY id
                        """,
                        (purchase_id,),
                    ).fetchall()

                    for photo in photo_rows:
                        raw_path = photo["file_path"]

                        if not raw_path:
                            continue

                        old_path = Path(str(raw_path))

                        if not old_path.is_absolute():
                            old_path = APP_DIR / old_path

                        if not old_path.exists():
                            continue

                        target_dir = (
                            PHOTOS_DIR
                            / f"supplier_{supplier_id}"
                            / f"purchase_{purchase_id}"
                        )

                        target_dir.mkdir(parents=True, exist_ok=True)

                        target = target_dir / old_path.name
                        counter = 1

                        while target.exists():
                            if target.resolve() == old_path.resolve():
                                break

                            target = target_dir / f"{counter}_{old_path.name}"
                            counter += 1

                        try:
                            if old_path.resolve() != target.resolve():
                                shutil.copy2(old_path, target)

                            photos.append(str(target.relative_to(APP_DIR)))

                        except Exception:
                            log.warning(
                                "Eski fotoğraf kopyalanamadı: %s", old_path
                            )
                            continue

                data["purchases"].append({
                    "id": purchase_id,
                    "supplier_id": supplier_id,
                    "date": str(purchase_row["purchase_date"]),
                    "total": float(purchase_row["total_amount"] or 0),
                    "description": purchase_row["description"] or "",
                    "items": items,
                    "photos": photos,
                    "created_at": purchase_row["created_at"] or datetime.now().isoformat(),
                })

            data = normalize_data(data)
            self.save(data)

            log.info(
                "Eski SQLite veritabanından %d tedarikçi, %d alış aktarıldı.",
                len(data["suppliers"]),
                len(data["purchases"]),
            )

        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


# ============================================================
# SUPPLIER DIALOG
# ============================================================

class SupplierDialog(QDialog):

    def __init__(self, parent=None, title="Yeni Tedarikçi"):
        super().__init__(parent)

        self.setWindowTitle(title)
        self.resize(500, 330)

        self.name = QLineEdit()
        self.phone = QLineEdit()
        self.address = QLineEdit()
        self.notes = QTextEdit()

        self.name.setPlaceholderText("Tedarikçi adı")
        self.phone.setPlaceholderText("Telefon")
        self.address.setPlaceholderText("Adres")

        form = QFormLayout()
        form.addRow("Adı *", self.name)
        form.addRow("Telefon", self.phone)
        form.addRow("Adres", self.address)
        form.addRow("Notlar", self.notes)

        save = QPushButton("Kaydet")
        cancel = QPushButton("İptal")

        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)

    def values(self):
        return {
            "name": self.name.text().strip(),
            "phone": self.phone.text().strip(),
            "address": self.address.text().strip(),
            "notes": self.notes.toPlainText().strip(),
        }


# ============================================================
# PURCHASE DIALOG
# ============================================================

class PurchaseDialog(QDialog):

    def __init__(self, supplier_name, parent=None, purchase=None):
        super().__init__(parent)

        self.purchase = purchase
        self.selected_photos = []
        self.removed_existing_photos = []

        title = (
            f"Alışı Düzenle - {supplier_name}"
            if purchase
            else f"Yeni Alış - {supplier_name}"
        )

        self.setWindowTitle(title)
        self.resize(850, 700)

        initial_date = (
            purchase.get("date", datetime.now().strftime("%Y-%m-%d"))
            if purchase
            else datetime.now().strftime("%Y-%m-%d")
        )

        self.date_edit = QLineEdit(str(initial_date))

        self.total = QDoubleSpinBox()
        self.total.setMaximum(999999999999)
        self.total.setDecimals(2)
        self.total.setSuffix(" TL")
        self.total.setValue(float(purchase.get("total", 0)) if purchase else 0)

        self.description = QTextEdit()
        if purchase:
            self.description.setPlainText(purchase.get("description", ""))

        # ----------------------------------------------------
        # PRODUCTS
        # ----------------------------------------------------

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Ürün", "Miktar", "Birim", "Fiyat"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        self.table.itemChanged.connect(self._update_calculated_total)

        add_item = QPushButton("+ Ürün Ekle")
        remove_item = QPushButton("Seçili Ürünü Sil")

        add_item.clicked.connect(self.add_item)
        remove_item.clicked.connect(self.remove_item)

        item_buttons = QHBoxLayout()
        item_buttons.addWidget(add_item)
        item_buttons.addWidget(remove_item)
        item_buttons.addStretch()

        self.calculated_total_label = QLabel("Ürünler toplamı: 0,00 TL")
        self.calculated_total_label.setStyleSheet("color: #555;")

        use_calculated_btn = QPushButton("Toplamı ürünlerden hesapla")
        use_calculated_btn.clicked.connect(self._apply_calculated_total)

        calc_row = QHBoxLayout()
        calc_row.addWidget(self.calculated_total_label)
        calc_row.addWidget(use_calculated_btn)
        calc_row.addStretch()

        # ----------------------------------------------------
        # PHOTOS
        # ----------------------------------------------------

        self.photo_list = QListWidget()
        self.photo_list.setSelectionMode(QAbstractItemView.ExtendedSelection)

        add_photo = QPushButton("📷 Fotoğraf Ekle")
        remove_photo = QPushButton("Fotoğrafı Listeden Çıkar")

        add_photo.clicked.connect(self.add_photos)
        remove_photo.clicked.connect(self.remove_photos)

        photo_buttons = QHBoxLayout()
        photo_buttons.addWidget(add_photo)
        photo_buttons.addWidget(remove_photo)
        photo_buttons.addStretch()

        if purchase:
            for photo in purchase.get("photos", []):
                item = QListWidgetItem(Path(photo).name)
                item.setData(Qt.UserRole, {"type": "existing", "path": photo})
                self.photo_list.addItem(item)

        # ----------------------------------------------------
        # BUTTONS
        # ----------------------------------------------------

        save = QPushButton("Kaydet")
        cancel = QPushButton("İptal")

        save.clicked.connect(self.validate_and_accept)
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        # ----------------------------------------------------
        # LAYOUT
        # ----------------------------------------------------

        form = QFormLayout()
        form.addRow("Tarih (YYYY-MM-DD)", self.date_edit)
        form.addRow("Toplam Tutar", self.total)
        form.addRow("Açıklama", self.description)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Ürünler"))
        layout.addLayout(item_buttons)
        layout.addWidget(self.table)
        layout.addLayout(calc_row)
        layout.addWidget(QLabel("Fotoğraflar"))
        layout.addWidget(self.photo_list)
        layout.addLayout(photo_buttons)
        layout.addLayout(buttons)

        if purchase:
            for item in purchase.get("items", []):
                row = self.table.rowCount()
                self.table.insertRow(row)

                self.table.setItem(row, 0, QTableWidgetItem(str(item.get("product_name", ""))))
                self.table.setItem(row, 1, QTableWidgetItem(str(item.get("quantity", 0))))
                self.table.setItem(row, 2, QTableWidgetItem(str(item.get("unit", ""))))
                self.table.setItem(row, 3, QTableWidgetItem(str(item.get("price", 0))))

        self._update_calculated_total()

    # ========================================================
    # TOPLAM HESAPLAMA (ürünler / manuel toplam tutarsızlığını önlemek için)
    # ========================================================

    def _sum_items(self):
        total = 0.0

        for row in range(self.table.rowCount()):
            quantity_item = self.table.item(row, 1)
            price_item = self.table.item(row, 3)

            try:
                quantity = float((quantity_item.text() if quantity_item else "0").replace(",", "."))
                price = float((price_item.text() if price_item else "0").replace(",", "."))
                total += quantity * price
            except (AttributeError, ValueError):
                continue

        return total

    def _update_calculated_total(self, *_args):
        total = self._sum_items()
        self.calculated_total_label.setText(f"Ürünler toplamı: {total:,.2f} TL")

    def _apply_calculated_total(self):
        self.total.setValue(self._sum_items())

    # ========================================================
    # PRODUCT
    # ========================================================

    def add_item(self):
        row = self.table.rowCount()
        self.table.insertRow(row)

        self.table.setItem(row, 0, QTableWidgetItem(""))
        self.table.setItem(row, 1, QTableWidgetItem("1"))
        self.table.setItem(row, 2, QTableWidgetItem("adet"))
        self.table.setItem(row, 3, QTableWidgetItem("0"))

        self.table.setCurrentCell(row, 0)
        self._update_calculated_total()

    def remove_item(self):
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )

        for row in rows:
            self.table.removeRow(row)

        self._update_calculated_total()

    # ========================================================
    # PHOTOS
    # ========================================================

    def add_photos(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Fotoğrafları Seç",
            "",
            "Resimler (*.jpg *.jpeg *.png *.webp *.bmp *.gif)",
        )

        for file_path in files:
            file_path = str(Path(file_path).resolve())

            if file_path in self.selected_photos:
                continue

            self.selected_photos.append(file_path)

            item = QListWidgetItem(Path(file_path).name)
            item.setData(Qt.UserRole, {"type": "new", "path": file_path})
            self.photo_list.addItem(item)

    def remove_photos(self):
        selected_items = self.photo_list.selectedItems()

        if not selected_items:
            return

        for item in selected_items:
            data = item.data(Qt.UserRole)

            if isinstance(data, dict):
                if data.get("type") == "new":
                    path = data.get("path")
                    if path in self.selected_photos:
                        self.selected_photos.remove(path)

                elif data.get("type") == "existing":
                    path = data.get("path")
                    if path not in self.removed_existing_photos:
                        self.removed_existing_photos.append(path)

            row = self.photo_list.row(item)
            self.photo_list.takeItem(row)

    # ========================================================
    # VALIDATE / VALUES
    # ========================================================

    def validate_and_accept(self):
        try:
            self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Hatalı Bilgi", str(exc))
            return

        self.accept()

    def values(self):
        try:
            purchase_date = datetime.strptime(
                self.date_edit.text().strip(), "%Y-%m-%d"
            ).date()
        except ValueError:
            raise ValueError("Tarih YYYY-MM-DD formatında olmalı.")

        items = []

        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            name = name_item.text().strip() if name_item else ""

            if not name:
                continue

            quantity_item = self.table.item(row, 1)
            price_item = self.table.item(row, 3)

            try:
                quantity = float((quantity_item.text() if quantity_item else "0").replace(",", "."))
                price = float((price_item.text() if price_item else "0").replace(",", "."))
            except (AttributeError, ValueError):
                raise ValueError("Ürün miktarı ve fiyatı sayı olmalı.")

            if quantity < 0:
                raise ValueError("Ürün miktarı negatif olamaz.")

            if price < 0:
                raise ValueError("Ürün fiyatı negatif olamaz.")

            unit_item = self.table.item(row, 2)
            unit = unit_item.text().strip() if unit_item else ""

            items.append({
                "product_name": name,
                "quantity": quantity,
                "unit": unit,
                "price": price,
            })

        return {
            "date": purchase_date.isoformat(),
            "total": float(self.total.value()),
            "description": self.description.toPlainText().strip(),
            "items": items,
            "photos": list(self.selected_photos),
            "removed_existing_photos": list(self.removed_existing_photos),
        }


# ============================================================
# PHOTO VIEWER
# ============================================================

class PhotoViewerDialog(QDialog):

    def __init__(self, photo_paths, parent=None):
        super().__init__(parent)

        self.photo_paths = list(photo_paths or [])

        self.setWindowTitle("Alış Fotoğrafları")
        self.resize(1000, 700)

        self.list_widget = QListWidget()

        self.preview = QLabel("Fotoğraf seçin")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(500, 450)
        self.preview.setStyleSheet(
            "background: #202020; color: white; border-radius: 6px;"
        )

        self.list_widget.currentRowChanged.connect(self.show_photo)

        layout = QHBoxLayout(self)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(self.preview, 4)

        for path in self.photo_paths:
            real_path = safe_relative_path(path)
            filename = real_path.name if real_path else Path(path).name
            self.list_widget.addItem(filename)

        if self.photo_paths:
            self.list_widget.setCurrentRow(0)

    def show_photo(self, row):
        if row < 0 or row >= len(self.photo_paths):
            self.preview.setText("Fotoğraf seçin")
            self.preview.setPixmap(QPixmap())
            return

        path = safe_relative_path(self.photo_paths[row])

        if path is None or not path.exists():
            self.preview.setText("Fotoğraf dosyası bulunamadı.")
            self.preview.setPixmap(QPixmap())
            return

        pixmap = QPixmap(str(path))

        if pixmap.isNull():
            self.preview.setText("Fotoğraf açılamadı.")
            return

        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.show_photo(self.list_widget.currentRow())


# ============================================================
# BACKUP MANAGER DIALOG (Yedekten Geri Yükle)
# ============================================================

class BackupManagerDialog(QDialog):

    def __init__(self, store: DataStore, parent=None):
        super().__init__(parent)

        self.store = store
        self.selected_path = None

        self.setWindowTitle("Yedekten Geri Yükle")
        self.resize(600, 420)

        self.list_widget = QListWidget()
        self.refresh_list()

        browse_btn = QPushButton("📂 Bilgisayardan Yedek Seç...")
        browse_btn.clicked.connect(self.browse_external)

        restore_btn = QPushButton("♻️ Seçili Yedeği Geri Yükle")
        restore_btn.clicked.connect(self.do_restore)

        cancel_btn = QPushButton("Kapat")
        cancel_btn.clicked.connect(self.reject)

        warning = QLabel(
            "⚠️ Geri yükleme, mevcut tüm verilerin ve fotoğrafların üzerine "
            "yazar.\nİşlemden hemen önce mevcut durumun otomatik bir "
            "güvenlik kopyası alınır."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #a15c00; padding: 6px;")

        buttons = QHBoxLayout()
        buttons.addWidget(browse_btn)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(restore_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Kayıtlı Yedekler</b>"))
        layout.addWidget(self.list_widget)
        layout.addWidget(warning)
        layout.addLayout(buttons)

    def refresh_list(self):
        self.list_widget.clear()

        for path in self.store.list_manual_backups():
            try:
                size_mb = path.stat().st_size / (1024 * 1024)
                mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%d.%m.%Y %H:%M"
                )
                label = f"{path.name}   ({size_mb:.1f} MB, {mtime})"
            except Exception:
                label = path.name

            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, str(path))
            self.list_widget.addItem(item)

        if self.list_widget.count() == 0:
            placeholder = QListWidgetItem("(Henüz kayıtlı yedek yok)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list_widget.addItem(placeholder)

    def browse_external(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Yedek Dosyası Seç", "", "ZIP Dosyası (*.zip)"
        )

        if file_path:
            item = QListWidgetItem(f"(Harici) {Path(file_path).name}")
            item.setData(Qt.UserRole, file_path)
            self.list_widget.addItem(item)
            self.list_widget.setCurrentItem(item)

    def do_restore(self):
        current = self.list_widget.currentItem()

        if not current or not current.data(Qt.UserRole):
            QMessageBox.information(self, "Bilgi", "Lütfen bir yedek seçin.")
            return

        answer = QMessageBox.question(
            self,
            "Geri Yükleme Onayı",
            (
                "Bu işlem mevcut tüm verilerin ve fotoğrafların üzerine\n"
                "seçili yedekteki verileri yazacak.\n\n"
                "Devam etmek istiyor musunuz?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            return

        self.selected_path = current.data(Qt.UserRole)
        self.accept()


# ============================================================
# MAIN WINDOW
# ============================================================

class MainWindow(QMainWindow):

    def __init__(self, store: DataStore):
        super().__init__()

        self.store = store
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1250, 750)

        self.current_supplier_id = None

        self._load_initial_data()

        self.build_ui()
        self.load_suppliers()

    # ========================================================
    # STARTUP
    # ========================================================

    def _load_initial_data(self):
        try:
            self.data = self.store.load()
        except AppError as exc:
            report_exception(self, "Veri Yükleme Hatası", exc)
            self.data = empty_data()

        if self.store.last_migration_error is not None:
            QMessageBox.warning(
                self,
                "Veri Aktarımı Uyarısı",
                (
                    "Eski SQLite verileriniz otomatik olarak aktarılmaya "
                    "çalışıldı ancak bir sorunla karşılaşıldı. Uygulama "
                    "yine de açılacak.\n\n"
                    f"Detaylar log dosyasında: {LOGS_DIR / 'app.log'}"
                ),
            )

        # Yetim fotoğraf klasörü/dosyalarını temizle (WinError 5 vb.
        # yüzünden önceki oturumlarda tamamlanamamış silmeleri tamamlar).
        try:
            cleanup_orphan_photos(self.data)
            cleanup_trash()
        except Exception:
            log.warning("Başlangıç temizliği sırasında hata.", exc_info=True)

    # ========================================================
    # UI
    # ========================================================

    def build_ui(self):

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎 Tedarikçi ara...")
        self.search.textChanged.connect(self.load_suppliers)

        self.supplier_list = QListWidget()
        self.supplier_list.itemClicked.connect(self.select_supplier)
        self.supplier_list.itemDoubleClicked.connect(self.open_supplier)

        add_supplier = QPushButton("+ Yeni Tedarikçi")
        edit_supplier = QPushButton("✏️ Tedarikçiyi Düzenle")
        delete_supplier = QPushButton("🗑 Tedarikçiyi Sil")

        add_supplier.clicked.connect(self.add_supplier)
        edit_supplier.clicked.connect(self.edit_supplier)
        delete_supplier.clicked.connect(self.delete_supplier)

        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Tedarikçiler</b>"))
        left.addWidget(self.search)
        left.addWidget(self.supplier_list)
        left.addWidget(add_supplier)
        left.addWidget(edit_supplier)
        left.addWidget(delete_supplier)

        left_widget = QWidget()
        left_widget.setLayout(left)

        # ----------------------------------------------------
        # Right
        # ----------------------------------------------------

        self.detail_title = QLabel("Bir tedarikçi seçin")
        self.detail_title.setStyleSheet(
            "font-size: 24px; font-weight: bold; padding: 5px;"
        )

        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(
            "padding: 8px; background: #f5f5f5; border-radius: 6px;"
        )

        new_purchase = QPushButton("+ Yeni Alış")
        new_purchase.clicked.connect(self.add_purchase)

        self.purchase_table = QTableWidget(0, 5)
        self.purchase_table.setHorizontalHeaderLabels(
            ["Tarih", "Toplam", "Ürünler", "Fotoğraf", "Durum"]
        )
        self.purchase_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.purchase_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.purchase_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.purchase_table.doubleClicked.connect(self.open_purchase)

        header = self.purchase_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        backup_btn = QPushButton("💾 Yedek Oluştur")
        backup_btn.clicked.connect(self.create_backup)

        restore_btn = QPushButton("🔄 Yedekten Geri Yükle")
        restore_btn.clicked.connect(self.restore_backup)

        backup_row = QHBoxLayout()
        backup_row.addWidget(backup_btn)
        backup_row.addWidget(restore_btn)

        right = QVBoxLayout()
        right.addWidget(self.detail_title)
        right.addWidget(self.info)
        right.addWidget(new_purchase)
        right.addWidget(self.purchase_table)
        right.addLayout(backup_row)

        right_widget = QWidget()
        right_widget.setLayout(right)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        self.setCentralWidget(splitter)

    # ========================================================
    # SAVE HELPER (tüm mutasyonlar bu tek noktadan geçer)
    # ========================================================

    def _persist(self, error_title="Kayıt Hatası"):
        """
        self.data'yı kaydeder. Başarılı olursa normalize edilmiş veriyi
        self.data'ya geri yazar ve True döner. Başarısız olursa kullanıcıya
        hata gösterir ve False döner (çağıran taraf işlemi geri almalı).
        """

        try:
            self.data = self.store.save(self.data)
            return True
        except AppError as exc:
            report_exception(self, error_title, exc)
            return False

    # ========================================================
    # SUPPLIERS
    # ========================================================

    def load_suppliers(self):
        self.supplier_list.clear()

        query = self.search.text().strip().lower()

        suppliers = sorted(
            self.data.get("suppliers", []),
            key=lambda x: x.get("name", "").lower(),
        )

        for supplier in suppliers:
            name = supplier.get("name", "")

            if query and query not in name.lower():
                continue

            supplier_id = int(supplier.get("id", -1))

            purchase_count = sum(
                1
                for purchase in self.data.get("purchases", [])
                if int(purchase.get("supplier_id", -1)) == supplier_id
            )

            item = QListWidgetItem(f"{name} ({purchase_count} alış)")
            item.setData(Qt.UserRole, supplier_id)
            self.supplier_list.addItem(item)

    def select_supplier(self, item):
        self.current_supplier_id = item.data(Qt.UserRole)
        self.load_supplier_detail()

    def open_supplier(self, item):
        self.select_supplier(item)

    def get_supplier(self):
        if self.current_supplier_id is None:
            return None

        try:
            selected_id = int(self.current_supplier_id)
        except (TypeError, ValueError):
            return None

        for supplier in self.data.get("suppliers", []):
            try:
                supplier_id = int(supplier.get("id", -1))
            except (TypeError, ValueError):
                continue

            if supplier_id == selected_id:
                return supplier

        return None

    # ========================================================
    # SUPPLIER DETAIL
    # ========================================================

    def load_supplier_detail(self):
        supplier = self.get_supplier()

        if not supplier:
            self.detail_title.setText("Bir tedarikçi seçin")
            self.info.clear()
            self.purchase_table.setRowCount(0)
            return

        supplier_id = int(supplier["id"])

        self.detail_title.setText(supplier.get("name", ""))

        self.info.setText(
            f"Telefon: {supplier.get('phone') or '-'}\n"
            f"Adres: {supplier.get('address') or '-'}\n"
            f"Not: {supplier.get('notes') or '-'}"
        )

        purchases = [
            purchase
            for purchase in self.data.get("purchases", [])
            if int(purchase.get("supplier_id", -1)) == supplier_id
        ]

        purchases.sort(key=lambda x: x.get("date", ""), reverse=True)

        self.purchase_table.setRowCount(0)

        for purchase in purchases:
            row = self.purchase_table.rowCount()
            self.purchase_table.insertRow(row)

            try:
                date = datetime.fromisoformat(
                    str(purchase.get("date", ""))
                ).strftime("%d.%m.%Y")
            except Exception:
                date = str(purchase.get("date", ""))

            total = f"{float(purchase.get('total', 0)):,.2f} TL"

            products = ", ".join(
                f"{item.get('product_name', '-')} "
                f"({float(item.get('quantity', 0)):g} {item.get('unit', '')})"
                for item in purchase.get("items", [])
            )

            photo_count = len(purchase.get("photos", []))

            self.purchase_table.setItem(row, 0, QTableWidgetItem(date))
            self.purchase_table.setItem(row, 1, QTableWidgetItem(total))
            self.purchase_table.setItem(row, 2, QTableWidgetItem(products or "-"))
            self.purchase_table.setItem(
                row, 3, QTableWidgetItem(f"📷 {photo_count}" if photo_count else "-")
            )
            self.purchase_table.setItem(row, 4, QTableWidgetItem("Kayıtlı"))

            self.purchase_table.item(row, 0).setData(
                Qt.UserRole, purchase.get("id")
            )

    # ========================================================
    # ADD SUPPLIER
    # ========================================================

    def add_supplier(self):
        dialog = SupplierDialog(self, "Yeni Tedarikçi")

        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.values()

        if not values["name"]:
            QMessageBox.warning(self, "Eksik Bilgi", "Tedarikçi adı zorunlu.")
            return

        duplicate = any(
            supplier.get("name", "").strip().lower() == values["name"].lower()
            for supplier in self.data.get("suppliers", [])
        )

        if duplicate:
            answer = QMessageBox.question(
                self,
                "Benzer Tedarikçi",
                (
                    "Aynı isimde bir tedarikçi zaten bulunuyor.\n\n"
                    "Yine de oluşturmak istiyor musunuz?"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )

            if answer != QMessageBox.Yes:
                return

        supplier = {
            "id": get_next_id(self.data, "next_supplier_id"),
            **values,
            "created_at": datetime.now().isoformat(),
        }

        self.data["suppliers"].append(supplier)

        if not self._persist("Tedarikçi Kayıt Hatası"):
            self.data["suppliers"] = [
                s for s in self.data["suppliers"] if s is not supplier
            ]
            return

        self.current_supplier_id = supplier["id"]
        self.load_suppliers()
        self.load_supplier_detail()

    # ========================================================
    # EDIT SUPPLIER
    # ========================================================

    def edit_supplier(self):
        supplier = self.get_supplier()

        if not supplier:
            QMessageBox.information(
                self, "Bilgi", "Düzenlemek için önce bir tedarikçi seçin."
            )
            return

        dialog = SupplierDialog(self, "Tedarikçiyi Düzenle")
        dialog.name.setText(supplier.get("name", ""))
        dialog.phone.setText(supplier.get("phone", ""))
        dialog.address.setText(supplier.get("address", ""))
        dialog.notes.setPlainText(supplier.get("notes", ""))

        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.values()

        if not values["name"]:
            QMessageBox.warning(self, "Eksik Bilgi", "Tedarikçi adı zorunlu.")
            return

        previous = dict(supplier)

        supplier["name"] = values["name"]
        supplier["phone"] = values["phone"]
        supplier["address"] = values["address"]
        supplier["notes"] = values["notes"]
        supplier["updated_at"] = datetime.now().isoformat()

        if not self._persist("Tedarikçi Güncelleme Hatası"):
            supplier.clear()
            supplier.update(previous)
            return

        self.load_suppliers()
        self.load_supplier_detail()

        QMessageBox.information(
            self, "Başarılı", f'"{supplier["name"]}" tedarikçisi güncellendi.'
        )

    # ========================================================
    # DELETE SUPPLIER
    # ========================================================

    def delete_supplier(self):
        supplier = self.get_supplier()

        if not supplier:
            QMessageBox.information(
                self, "Bilgi", "Silmek için önce bir tedarikçi seçin."
            )
            return

        supplier_id = int(supplier["id"])
        supplier_name = supplier.get("name", "Bilinmeyen")

        purchases = [
            purchase
            for purchase in self.data.get("purchases", [])
            if int(purchase.get("supplier_id", -1)) == supplier_id
        ]

        answer = QMessageBox.question(
            self,
            "Tedarikçiyi Sil",
            (
                f'"{supplier_name}" tedarikçisi silinecek.\n\n'
                f"Alış kayıtları: {len(purchases)}\n\n"
                "Bu işlem geri alınamaz.\n\n"
                "Devam etmek istiyor musunuz?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            return

        supplier_photo_dir = PHOTOS_DIR / f"supplier_{supplier_id}"

        # ----------------------------------------------------
        # 1) ÖNCE JSON: fotoğraf dosyasının kilitli olması veri
        #    silme işlemini asla engellemez.
        # ----------------------------------------------------

        old_suppliers = list(self.data.get("suppliers", []))
        old_purchases = list(self.data.get("purchases", []))

        self.data["purchases"] = [
            purchase
            for purchase in old_purchases
            if int(purchase.get("supplier_id", -1)) != supplier_id
        ]

        self.data["suppliers"] = [
            item for item in old_suppliers if int(item.get("id", -1)) != supplier_id
        ]

        if not self._persist("Tedarikçi Silme Hatası"):
            self.data["suppliers"] = old_suppliers
            self.data["purchases"] = old_purchases
            return

        # ----------------------------------------------------
        # 2) SONRA fiziksel dosyalar. Başarısız olsa bile JSON
        #    zaten güncel; klasör bir sonraki açılışta otomatik
        #    yetim taramasıyla temizlenecek.
        # ----------------------------------------------------

        photo_delete_ok = True

        if supplier_photo_dir.exists():
            photo_delete_ok = move_to_trash(supplier_photo_dir)

        self.current_supplier_id = None
        self.detail_title.setText("Bir tedarikçi seçin")
        self.info.clear()
        self.purchase_table.setRowCount(0)
        self.load_suppliers()

        if photo_delete_ok:
            QMessageBox.information(
                self, "Başarılı", f'"{supplier_name}" tedarikçisi başarıyla silindi.'
            )
        else:
            QMessageBox.warning(
                self,
                "Silme Tamamlandı",
                (
                    f'"{supplier_name}" tedarikçisi ve alış kayıtları silindi.\n\n'
                    "Ancak bazı fotoğraf dosyaları şu anda başka bir uygulama "
                    "tarafından kullanıldığı için hemen temizlenemedi.\n\n"
                    "Uygulama bir sonraki açılışta bunları otomatik olarak "
                    "temizlemeyi tekrar deneyecek."
                ),
            )

    # ========================================================
    # ADD PURCHASE
    # ========================================================

    def add_purchase(self):
        supplier = self.get_supplier()

        if not supplier:
            QMessageBox.information(self, "Bilgi", "Önce bir tedarikçi seçin.")
            return

        dialog = PurchaseDialog(supplier.get("name", ""), self)

        if dialog.exec() != QDialog.Accepted:
            return

        try:
            values = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Hatalı Bilgi", str(exc))
            return

        purchase_id = get_next_id(self.data, "next_purchase_id")

        purchase = {
            "id": purchase_id,
            "supplier_id": int(supplier["id"]),
            "date": values["date"],
            "total": values["total"],
            "description": values["description"],
            "items": values["items"],
            "photos": [],
            "created_at": datetime.now().isoformat(),
        }

        photos = values.get("photos", [])
        purchase_dir = (
            PHOTOS_DIR / f"supplier_{supplier['id']}" / f"purchase_{purchase_id}"
        )

        if photos:
            purchase_dir.mkdir(parents=True, exist_ok=True)

            for index, source in enumerate(photos, start=1):
                source_path = Path(source)

                if not source_path.exists():
                    continue

                destination = purchase_dir / f"{index}_{source_path.name}"

                try:
                    shutil.copy2(source_path, destination)
                    purchase["photos"].append(relative_app_path(destination))
                except Exception as exc:
                    log.warning("Fotoğraf kopyalanamadı: %s", source_path, exc_info=True)
                    QMessageBox.warning(
                        self,
                        "Fotoğraf Uyarısı",
                        f'"{source_path.name}" kopyalanamadı.\n\n{exc}',
                    )

        self.data["purchases"].append(purchase)

        if not self._persist("Alış Kayıt Hatası"):
            # JSON kaydedilemedi: RAM'den çıkar, kopyalanan fotoğrafları
            # da temizlemeyi dene (başarısız olsa bile bir sonraki
            # açılışta yetim taraması yakalar).
            self.data["purchases"] = [
                item for item in self.data["purchases"] if item is not purchase
            ]

            if photos and purchase_dir.exists():
                remove_directory_safely(purchase_dir, retries=1)

            return

        self.load_supplier_detail()
        self.load_suppliers()

    # ========================================================
    # EDIT PURCHASE
    # ========================================================

    def edit_purchase(self, purchase_id):
        try:
            purchase_id = int(purchase_id)
        except (TypeError, ValueError):
            QMessageBox.warning(self, "Hata", "Alış ID bilgisi geçersiz.")
            return

        purchase = next(
            (
                p
                for p in self.data.get("purchases", [])
                if int(p.get("id", -1)) == purchase_id
            ),
            None,
        )

        if not purchase:
            QMessageBox.warning(self, "Hata", "Alış kaydı bulunamadı.")
            return

        supplier_id = int(purchase.get("supplier_id", -1))

        supplier = next(
            (
                s
                for s in self.data.get("suppliers", [])
                if int(s.get("id", -1)) == supplier_id
            ),
            None,
        )

        if not supplier:
            QMessageBox.warning(self, "Hata", "Bu alışın tedarikçisi bulunamadı.")
            return

        dialog = PurchaseDialog(supplier.get("name", ""), self, purchase)

        if dialog.exec() != QDialog.Accepted:
            return

        try:
            values = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Hatalı Bilgi", str(exc))
            return

        old_photos = list(purchase.get("photos", []))
        previous_purchase_snapshot = dict(purchase)

        new_photos = [
            photo
            for photo in old_photos
            if photo not in values.get("removed_existing_photos", [])
        ]

        new_files = values.get("photos", [])

        purchase_dir = (
            PHOTOS_DIR / f"supplier_{supplier_id}" / f"purchase_{purchase_id}"
        )

        if new_files:
            purchase_dir.mkdir(parents=True, exist_ok=True)

            for source in new_files:
                source_path = Path(source)

                if not source_path.exists():
                    continue

                base_name = source_path.name
                destination = purchase_dir / base_name
                counter = 1

                while destination.exists():
                    destination = purchase_dir / f"{counter}_{base_name}"
                    counter += 1

                try:
                    shutil.copy2(source_path, destination)
                    new_photos.append(relative_app_path(destination))
                except Exception as exc:
                    log.warning("Fotoğraf kopyalanamadı: %s", base_name, exc_info=True)
                    QMessageBox.warning(
                        self,
                        "Fotoğraf Uyarısı",
                        f'"{base_name}" kopyalanamadı.\n\n{exc}',
                    )

        purchase["date"] = values["date"]
        purchase["total"] = values["total"]
        purchase["description"] = values["description"]
        purchase["items"] = values["items"]
        purchase["photos"] = new_photos
        purchase["updated_at"] = datetime.now().isoformat()

        if not self._persist("Alış Güncelleme Hatası"):
            purchase.clear()
            purchase.update(previous_purchase_snapshot)
            return

        # ----------------------------------------------------
        # JSON güncellendi; şimdi kullanıcının listeden çıkardığı
        # fotoğrafları fiziksel olarak sil.
        # ----------------------------------------------------

        for photo in values.get("removed_existing_photos", []):
            real_path = safe_relative_path(photo)

            if real_path is None:
                continue

            if not is_inside_directory(real_path, PHOTOS_DIR):
                continue

            remove_file_safely(real_path)

        if purchase_dir.exists():
            try:
                if not any(purchase_dir.iterdir()):
                    remove_directory_safely(purchase_dir)
            except Exception:
                pass

        self.load_supplier_detail()
        self.load_suppliers()

        QMessageBox.information(self, "Başarılı", "Alış kaydı güncellendi.")

    # ========================================================
    # DELETE PURCHASE
    # ========================================================

    def delete_purchase(self, purchase_id):
        try:
            purchase_id = int(purchase_id)
        except (TypeError, ValueError):
            return

        purchase = next(
            (
                p
                for p in self.data.get("purchases", [])
                if int(p.get("id", -1)) == purchase_id
            ),
            None,
        )

        if not purchase:
            return

        answer = QMessageBox.question(
            self,
            "Alışı Sil",
            (
                "Bu alış kaydı silinecek.\n\n"
                "Ürünler, açıklama ve kayıt bilgileri silinir.\n"
                "Bağlı fotoğraflar da temizlenir.\n\n"
                "Devam etmek istiyor musunuz?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            return

        old_purchases = list(self.data.get("purchases", []))

        self.data["purchases"] = [
            p for p in old_purchases if int(p.get("id", -1)) != purchase_id
        ]

        if not self._persist("Alış Silme Hatası"):
            self.data["purchases"] = old_purchases
            return

        supplier_id = int(purchase.get("supplier_id", -1))

        purchase_dir = (
            PHOTOS_DIR / f"supplier_{supplier_id}" / f"purchase_{purchase_id}"
        )

        photo_delete_ok = True

        if purchase_dir.exists():
            photo_delete_ok = move_to_trash(purchase_dir)

        self.load_supplier_detail()
        self.load_suppliers()

        if photo_delete_ok:
            QMessageBox.information(self, "Başarılı", "Alış kaydı silindi.")
        else:
            QMessageBox.warning(
                self,
                "Silme Tamamlandı",
                (
                    "Alış kaydı silindi.\n\n"
                    "Ancak fotoğraf klasörü şu anda kullanıldığı için henüz "
                    "temizlenemedi; bir sonraki açılışta otomatik temizlenecek."
                ),
            )

    # ========================================================
    # OPEN PURCHASE
    # ========================================================

    def open_purchase(self, *_args):
        row = self.purchase_table.currentRow()

        if row < 0:
            return

        table_item = self.purchase_table.item(row, 0)

        if table_item is None:
            return

        try:
            purchase_id = int(table_item.data(Qt.UserRole))
        except (TypeError, ValueError):
            QMessageBox.warning(self, "Hata", "Alış ID bilgisi okunamadı.")
            return

        purchase = next(
            (
                p
                for p in self.data.get("purchases", [])
                if int(p.get("id", -1)) == purchase_id
            ),
            None,
        )

        if purchase is None:
            QMessageBox.warning(self, "Hata", "Alış kaydı bulunamadı.")
            return

        products = "\n".join(
            f"• {item.get('product_name', '-')}: "
            f"{float(item.get('quantity', 0)):g} {item.get('unit', '')} — "
            f"{float(item.get('price', 0)):,.2f} TL"
            for item in purchase.get("items", [])
        ) or "• Ürün yok"

        try:
            formatted_date = datetime.fromisoformat(
                str(purchase.get("date", ""))
            ).strftime("%d.%m.%Y")
        except Exception:
            formatted_date = str(purchase.get("date", ""))

        photos = purchase.get("photos", [])

        dialog = QDialog(self)
        dialog.setWindowTitle("Alış Detayı")
        dialog.resize(700, 650)

        info = QLabel(
            f"<b>Tarih:</b> {formatted_date}<br>"
            f"<b>Toplam:</b> {float(purchase.get('total', 0)):,.2f} TL<br><br>"
            f"<b>Ürünler:</b><br>{products.replace(chr(10), '<br>')}<br><br>"
            f"<b>Açıklama:</b><br>{purchase.get('description') or '-'}"
        )
        info.setWordWrap(True)

        photo_label = QLabel(f"📷 Fotoğraf sayısı: {len(photos)}")

        edit_button = QPushButton("✏️ Düzenle")
        view_photos = QPushButton("📷 Fotoğrafları Görüntüle")
        delete_button = QPushButton("🗑 Alışı Sil")
        close_button = QPushButton("Kapat")

        view_photos.setEnabled(bool(photos))

        def edit():
            dialog.accept()
            self.edit_purchase(purchase_id)

        def show_photos():
            PhotoViewerDialog(photos, dialog).exec()

        def delete():
            dialog.accept()
            self.delete_purchase(purchase_id)

        edit_button.clicked.connect(edit)
        view_photos.clicked.connect(show_photos)
        delete_button.clicked.connect(delete)
        close_button.clicked.connect(dialog.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(edit_button)
        buttons.addWidget(view_photos)
        buttons.addStretch()
        buttons.addWidget(delete_button)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(dialog)
        layout.addWidget(info)
        layout.addWidget(photo_label)
        layout.addStretch()
        layout.addLayout(buttons)

        dialog.exec()

    # ========================================================
    # BACKUP / RESTORE
    # ========================================================

    def create_backup(self):
        try:
            backup_path = self.store.create_backup()
        except AppError as exc:
            report_exception(self, "Yedekleme Hatası", exc)
            return

        answer = QMessageBox.question(
            self,
            "Yedek Oluşturuldu",
            (
                "Yedek başarıyla oluşturuldu:\n\n"
                f"{backup_path}\n\n"
                "Bu yedek uygulama içinde saklanır ve 'Yedekten Geri Yükle' "
                "ile kullanılabilir. Ayrıca bir kopyasını başka bir konuma "
                "(örn. USB bellek) da kaydetmek ister misiniz?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            return

        target, _ = QFileDialog.getSaveFileName(
            self, "Yedeği Farklı Kaydet", backup_path.name, "ZIP Dosyası (*.zip)"
        )

        if not target:
            return

        try:
            shutil.copy2(backup_path, target)
            QMessageBox.information(self, "Başarılı", f"Yedek kopyalandı:\n{target}")
        except Exception as exc:
            log.exception("Yedek dışa kopyalanamadı.")
            QMessageBox.warning(
                self,
                "Kopyalama Uyarısı",
                (
                    "Yedek uygulama içinde başarıyla oluşturuldu ancak "
                    f"seçtiğiniz konuma kopyalanamadı.\n\n{exc}"
                ),
            )

    def restore_backup(self):
        dialog = BackupManagerDialog(self.store, self)

        if dialog.exec() != QDialog.Accepted or not dialog.selected_path:
            return

        try:
            self.data = self.store.restore_from_zip(dialog.selected_path)
        except AppError as exc:
            report_exception(self, "Geri Yükleme Hatası", exc)
            return

        try:
            cleanup_orphan_photos(self.data)
            cleanup_trash()
        except Exception:
            log.warning("Restore sonrası temizlik hatası.", exc_info=True)

        self.current_supplier_id = None
        self.load_suppliers()
        self.load_supplier_detail()

        QMessageBox.information(
            self, "Başarılı", "Veriler seçilen yedekten geri yüklendi."
        )

    # ========================================================
    # CLOSE
    # ========================================================

    def closeEvent(self, event):
        # Tüm mutasyonlar zaten anında kaydedildiği için kapanışta ek bir
        # yazma işlemine gerek yok; sadece log handler'larını temizce
        # kapatıyoruz ki log dosyası flush edilsin.
        for handler in list(log.handlers):
            try:
                handler.flush()
            except Exception:
                pass

        super().closeEvent(event)


# ============================================================
# MAIN
# ============================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")

    try:
        bootstrap()
    except Exception as exc:
        QMessageBox.critical(
            None,
            "Başlangıç Hatası",
            (
                "Uygulama gerekli klasörleri oluşturamadı, bu yüzden "
                "başlatılamıyor.\n\n"
                f"{exc}\n\n"
                "Lütfen uygulamayı yazma izni olan bir klasörde çalıştırın."
            ),
        )
        sys.exit(1)

    install_excepthook()

    store = DataStore()
    window = MainWindow(store)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
