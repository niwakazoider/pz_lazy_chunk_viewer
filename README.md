# PZ B42 Lazy Chunk Viewer

Project Zomboid Build 42 の static map / save data を読み込み、8×8 chunk 単位で遅延レンダリングするローカルマップビューアです。

※ 現在は save の **main sprite のみ**描画し、extra sprites は無効にしています。

## Requirements

- Python 3.10+
- Pillow
- PyYAML

```bash
pip install -r requirements.txt
```

## Usage

`config.example.json` を `config.json` にコピーして、Project Zomboid のパスを設定します。

```bash
python server.py
```

その後ブラウザで:

```text
http://127.0.0.1:8080/
```

## References

このプロジェクトの save data / map data 解析では、以下のプロジェクトを参照しています。

- [pzdataspec](https://github.com/cff29546/pzdataspec)  
  Project Zomboid の save / game data 用 Kaitai Struct schema と parser tools。

- [PZ_Mapping_Tools](https://github.com/Unjammer/PZ_Mapping_Tools)  
  Project Zomboid の WorldEd / TileZed / BuildingEd 系 mapping tools。

- [PZ Mapping Tools: Community Edition](https://github.com/gruxor/pz-tools-ce)  
  Build 42 対応を含む community edition。

