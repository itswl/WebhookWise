import contextlib
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.append(str(Path(__file__).parent.parent))

from services.data_maintenance import archive_old_data

if __name__ == "__main__":
    # 获取归档天数参数
    archive_days = 30
    if len(sys.argv) > 1:
        with contextlib.suppress(ValueError):
            archive_days = int(sys.argv[1])
            
    archive_old_data(archive_days)
