import os
import shutil
from modelscope import snapshot_download

model_id = 'SaaRaaS/Savant4RedT-1_8B-Content'
target_path = 'models'
expected_model_subpath = 'Savant4RedT-1_8B-Content'
expected_path = os.path.join(target_path, expected_model_subpath)

if not os.path.exists(expected_path) or not os.listdir(expected_path):
    downloaded_path = snapshot_download(model_id, cache_dir=target_path)

    if not os.path.isdir(downloaded_path):
        raise ValueError(f"Expected {downloaded_path} to be a directory.")

    if os.path.exists(expected_path):
        if os.path.isdir(expected_path):
            shutil.rmtree(expected_path)
        else:
            os.remove(expected_path)

    shutil.move(downloaded_path, expected_path)

os.system("streamlit run 😀_Beginning.py --server.address 0.0.0.0 --server.port 7860")
