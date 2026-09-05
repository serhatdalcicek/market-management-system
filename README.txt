# 🛒 Market Management System

A simple and user-friendly desktop application developed for managing market suppliers and purchase records.

The application allows users to keep supplier information, record purchases, store product details, and attach purchase photos. All data is stored locally on the user's computer, so no cloud service or internet connection is required.

## ✨ Features

* 👤 **Supplier Management**

  * Add suppliers
  * Edit supplier information
  * Delete suppliers
  * Search suppliers

* 🛒 **Purchase Management**

  * Add new purchase records
  * Edit existing purchases
  * View purchase details
  * Store purchase date and total amount
  * Add products, quantities, units and prices
  * Add purchase descriptions

* 📷 **Photo Management**

  * Attach photos to purchase records
  * Store photos locally
  * View attached photos
  * Keep photos associated with the corresponding supplier and purchase

* 💾 **Local Data Storage**

  * No cloud database required
  * Data is stored locally in JSON format
  * Photos are stored in the local application directory
  * Data remains available after closing and reopening the application

* 📦 **Backup**

  * Create ZIP backups containing application data and photos

## 🖥️ Application Flow

The application is designed to be simple enough for users without technical knowledge.

```text
Market.exe
    ↓
Select Supplier
    ↓
Add Purchase
    ↓
Add Products
    ↓
Add Photos
    ↓
Save
```

Existing purchases can also be opened and edited later.

## 🛠️ Technologies

* Python
* PySide6
* JSON
* SQLite / Python `sqlite3` for legacy data migration
* PyInstaller for Windows executable builds

## 📁 Data Structure

The application stores its data locally.

```text
Market/
│
├── Market.exe
│
└── market_data/
    ├── data.json
    │
    └── photos/
        ├── supplier_1/
        │   ├── purchase_1/
        │   │   ├── 1_photo.jpg
        │   │   └── 2_photo.jpg
        │   │
        │   └── purchase_2/
        │
        └── supplier_2/
```

### `data.json`

Contains:

* Supplier information
* Purchase records
* Product information
* Purchase dates
* Purchase totals
* Descriptions
* Photo references

### `photos/`

Contains the actual images associated with purchase records.

## 💻 Installation

### For End Users

The application can be distributed as a Windows executable.

```text
Setup.exe
    ↓
Next
    ↓
Next
    ↓
Install
    ↓
Desktop → Market
```

The end user does not need to install Python or configure a database.

## 🔨 Building the Application

The project can be converted into a Windows executable using PyInstaller.

Example:

```bash
pyinstaller --noconfirm --clean --windowed --name Market main.py
```

For a distributable installer, the generated application can then be packaged with an installer such as Inno Setup.

## 💾 Backup & Data Safety

The application provides a backup feature that creates a `.zip` file containing:

* `data.json`
* All stored purchase photos

Users can use this ZIP file to keep a copy of their market records.

## 🔄 Legacy Database Migration

Earlier versions of the application used SQLite/SQLAlchemy.

The current version uses:

```text
JSON + Local Photos
```

If an old `data/market.db` database exists and no `market_data/data.json` exists yet, the application can migrate the existing records to the new JSON-based structure using Python's built-in `sqlite3` module.

This removes the need for SQLAlchemy in the current application.

## 🎯 Project Goal

The main goal of this project is to provide a lightweight and easy-to-use solution for small market businesses to manage:

**Suppliers → Purchases → Products → Photos**

without requiring a cloud service, complicated database management, or technical knowledge from the end user.

## 🚀 Future Improvements

Possible future improvements include:

* 📊 Purchase statistics
* 💰 Supplier-based spending reports
* 🔎 Advanced purchase search
* 📅 Date-based filtering
* 📷 Improved photo management
* 📤 Export reports to Excel/PDF
* 🔐 Optional application password
* 🔄 Improved backup and restore
* 📦 Automatic application updates

## 📄 License

This project is currently for personal/internal use.

License information can be added when the project is prepared for public distribution.
