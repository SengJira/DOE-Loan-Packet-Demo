import dtlpy as dl, os, fitz
dl.login_m2m(email=os.environ['DL_EMAIL'], password=os.environ['DL_PW'])
p = dl.projects.get(project_id='37b46e9c-3d18-475d-b624-254d6e5eefd3')
src = p.datasets.get(dataset_name='loan-packets')
try:
    dst = p.datasets.get(dataset_name='loan-packet-images')
    print('dataset exists')
except Exception:
    dst = p.datasets.create(dataset_name='loan-packet-images')
    print('created dataset')
items = list(src.items.list().all())
done = {i.name: i for i in dst.items.list().all()} if dst.items.list().items_count else {}
import io, time
n = 0
for it in items:
    png_name = it.name.rsplit('.', 1)[0] + '.png'
    remote = it.dir + '/' + png_name
    if any(i.filename == remote for i in done.values()):
        continue
    local = it.download('/tmp/in.pdf')
    doc = fitz.open(local)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    buf = io.BytesIO(pix.tobytes('png')); buf.name = png_name
    meta = dict(it.metadata.get('user', {}))
    dst.items.upload(local_path=buf, remote_path=it.dir, item_metadata={'user': meta}, overwrite=True)
    n += 1
    if n % 50 == 0: print('uploaded', n)
print('done', n, 'total', dst.items.list().items_count)
