#!/usr/bin/python3

'''
Copyright (c) 2025 Kota UCHIDA

The web server to interact with users.
'''

import argparse
from collections import namedtuple
import configparser
import http.server
from itertools import product
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import uuid

import jinja2

from . import diffimg
from . import repo

LOG_LEVELS = ['debug', 'info', 'warning', 'error', 'critical']
SCH_PROPERTY_PAT = re.compile(r'\(property\s+"(?P<name>[^"]+)"\s+"(?P<value>[^"]+)"')

KicadSheet = namedtuple('KicadSheet', ['name', 'file'])

logger = logging.getLogger(__name__)

def convert_path_to_windows(file_path):
    path_cmd = ['wslpath', '-w', file_path]
    res = subprocess.run(path_cmd, capture_output=True)
    res.check_returncode()
    return Path(res.stdout.decode('utf-8').strip())

def using_kicadwin_from_wsl(kicad_cli):
    '''
    KiCad for Windows を WSL から使う場合に真
    '''
    return Path('/usr/bin/wslpath').exists() and kicad_cli.endswith('.exe')

def export_svgs(dst_dir_path, mode, file_path, kicad_cli, layers):
    dst_dir_path_for_cmd = dst_dir_path
    file_path_for_cmd = file_path
    if using_kicadwin_from_wsl(kicad_cli):
        # パスの変換が必要
        if dst_dir_path.drive == '':
            dst_dir_path_for_cmd = convert_path_to_windows(dst_dir_path)
        if file_path.drive == '':
            file_path_for_cmd = convert_path_to_windows(file_path)

    export_cmd = [kicad_cli, mode, 'export', 'svg',
                  '--black-and-white', '--output', str(dst_dir_path_for_cmd)]
    if mode == 'pcb':
        export_cmd.extend(['--fit-page-to-board', '--mode-multi',
                           '--layers', ','.join(layers)])
    elif mode == 'sch':
        export_cmd.extend(['--no-background-color'])

    res = subprocess.run(export_cmd + [str(file_path_for_cmd)])
    if res.returncode != 0:
        logger.error('failed to export SVGs: args=%s', res.args)
        res.check_returncode()

    if mode == 'sch':
        '''
        sch export svgは <proj-name>.svg, <proj-name>-<sheet>.svg
        というファイルを生成するので、希望のファイル名に変えておく。
        '''
        sch_stem = file_path.stem
        svgs = list(dst_dir_path.glob(sch_stem + '*.svg'))
        logger.debug('globbed svg files in %s: %s', dst_dir_path, svgs)
        for svg in svgs:
            new_name = svg.name[len(sch_stem):]
            if new_name == '.svg': # top sheet
                new_name = svg.name
            elif new_name.startswith('-'):
                new_name = new_name[1:]
            else:
                logger.warning('unknown filename pattern: %s', svg.name)
                continue
            os.rename(svg, svg.parent / new_name)
            logger.debug('file renamed: %s => %s', svg, svg.parent / new_name)

def make_pcbsvg_filename(pcb_file_name, layer_name):
    l = layer_name.replace('.', '_')
    return f'{Path(pcb_file_name).stem}-{l}.svg'

#def make_schsvg_filename(sch_file_name):
#    return Path(sch_file_name).stem + '.svg'

def action_image(req, diff_base, diff_target, filename):
    if not filename.endswith('.svg'):
        req.send_error(http.HTTPStatus.NOT_FOUND)
        return

    obj = filename[:-4]
    mode = 'pcb' if obj in req.layers else 'sch'

    base_dir = req.tmp_dir_path / diff_base
    target_dir = req.tmp_dir_path / diff_target
    file_path = req.pcb_path if mode == 'pcb' else req.sch_path

    logger.debug('action_image: obj=%s mode=%s diff_base=%s diff_target=%s file_path=%s',
                 obj, mode, diff_base, diff_target, file_path)

    base_file_path = base_dir / file_path.name
    target_file_path = target_dir / file_path.name

    diff_target = None if diff_target == 'WORK' else diff_target
    req.kicad_repo.extract_file(diff_base, file_path.name, base_file_path)
    req.kicad_repo.extract_file(diff_target, file_path.name, target_file_path)

    if mode == 'sch':
        # 階層シートの回路図を持ってこないと階層シートの SVG が空になってしまう
        sheets = get_sch_subsheets_recursive(req.sch_path)
        for sheet in sheets:
            req.kicad_repo.extract_file(diff_base,
                                        sheet.file,
                                        base_dir / sheet.file)
            req.kicad_repo.extract_file(diff_target,
                                        sheet.file,
                                        target_dir / sheet.file)

    if mode == 'pcb':
        base_svg_path = base_dir / mode / make_pcbsvg_filename(file_path.name, obj)
        target_svg_path = target_dir / mode / make_pcbsvg_filename(file_path.name, obj)
    elif mode == 'sch':
        base_svg_path = base_dir / mode / (obj + '.svg')
        target_svg_path = target_dir / mode / (obj + '.svg')

    if not base_svg_path.exists():
        export_svgs(base_dir / mode, mode, base_file_path, req.kicad_cli, req.layers)
    if not target_svg_path.exists():
        export_svgs(target_dir / mode, mode, target_file_path, req.kicad_cli, req.layers)

    with open(base_svg_path) as f:
        base_svg = f.read()
    with open(target_svg_path) as f:
        target_svg = f.read()

    overlayed_svg = diffimg.overlay_two_svgs(base_svg, target_svg, False)
    svg_pos = overlayed_svg.find('<svg')
    if svg_pos < 0:
        logger.error('overlayed_svg does not contain <svg> tag: %s', overlayed_svg[:100])
        req.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR)
        return

    svg_pos += 4
    overlayed_svg = overlayed_svg[:svg_pos] + ' id="overlayed_svg"' + overlayed_svg[svg_pos:]

    encoded_svg = overlayed_svg.encode('utf-8')

    req.send_response(200)
    req.send_header('Content-Type', 'image/svg+xml')
    req.send_header('Content-Length', len(encoded_svg))
    req.end_headers()
    req.wfile.write(encoded_svg)

def get_sch_subsheets(sch_path):
    with open(sch_path) as f:
        sch_src = f.read()

    if not sch_src.startswith('(kicad_sch'):
        raise SyntaxError('kicad_sch has invalid syntax')

    sheets = []
    pos = 0
    while pos < len(sch_src):
        pos = sch_src.find('(sheet', pos)
        if pos < 0:
            break
        pos += 6
        if not sch_src[pos].isspace():
            # '(sheet_instances' などがヒットしたため、検索しなおす
            continue

        # '(sheet' に対応する閉じ括弧を探す
        paren = 1
        sheet_end_pos = -1
        for i in range(pos, len(sch_src)):
            if sch_src[i] == '(':
                paren += 1
            elif sch_src[i] == ')':
                paren -= 1

            if paren == 0: # end of sheet
                sheet_end_pos = i
                break
        if sheet_end_pos < 0:
            raise SyntaxError('sheet is not closed')

        sheetname = None
        sheetfile = None
        while pos < sheet_end_pos:
            m = SCH_PROPERTY_PAT.search(sch_src, pos, sheet_end_pos)
            if m is None:
                break
            pos = m.end()

            name = m.group('name')
            value = m.group('value')
            if name == 'Sheetname' or name == 'Sheet name':
                sheetname = value
            elif name == 'Sheetfile' or name == 'Sheet file':
                sheetfile = value

        pos = sheet_end_pos + 1

        if sheetname is None or sheetfile is None:
            raise SyntaxError('no "Sheetname" or "Sheetfile" in sheet object')

        sheets.append(KicadSheet(sheetname, sheetfile))

    return sheets

def get_sch_subsheets_recursive(sch_path):
    sheets = get_sch_subsheets(sch_path)

    sch_dir = sch_path.parent
    for sheet in sheets:
        sheets.extend(get_sch_subsheets_recursive(sch_dir / sheet.file))

    return sheets

def action_diff(req, diff_base, diff_target, obj):
    obj_list = req.layers

    if req.sch_path:
        sheets = get_sch_subsheets_recursive(req.sch_path)
        files = [sh.name for sh in sheets] + [req.sch_path]
        names = [Path(f).stem for f in files]
        obj_list = req.layers + names

    if obj not in obj_list:
        req.send_error(http.HTTPStatus.NOT_FOUND)
        return

    commit_logs = req.kicad_repo.git_repo.get_commit_logs()
    backup_versions = req.kicad_repo.backups_repo.get_versions()

    t = req.jinja_env.get_template('diffpcb.html')
    s = t.render(base_commit_id=diff_base,
                 target_commit_id=diff_target,
                 obj_list=obj_list,
                 layer=obj,
                 commit_logs=commit_logs,
                 backup_versions=backup_versions).encode('utf-8')
    req.send_response(200)
    req.send_header('Content-Type', 'text/html')
    req.send_header('Content-Length', len(s))
    req.end_headers()
    req.wfile.write(s)

class HTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 0.5
    '''
    ThreadingHTTPServerを使う方が本質的な解消ができると思うが、
    マルチスレッドの複雑さやログが入り乱れることを避けるために
    ソケットのタイムアウト設定で凌ぐことにする。
    '''

    def __init__(self, tmp_dir_path, kicad_repo, jinja_env, pcb_path, sch_path, kicad_cli, layers, *args, **kwargs):
        self.tmp_dir_path = tmp_dir_path
        self.kicad_repo = kicad_repo
        self.jinja_env = jinja_env
        self.pcb_path = pcb_path
        self.sch_path = sch_path
        self.kicad_cli = kicad_cli
        self.layers = layers
        self.traceid = uuid.uuid4()

        logger.debug('HTTPRequestHandler.__init__(%s): args=%s kwargs=%s', self.traceid, args, kwargs)

        # 親クラスの __init__ は、その中で do_x が実行されるため、最後に呼び出す
        super().__init__(*args, **kwargs)

    def do_GET(self):
        logger.info('do_GET path=%s traceid=%s', self.path, self.traceid)
        try:
            url_parts = urllib.parse.urlparse(self.path)

            if url_parts.path == '/':
                self.send_response(http.HTTPStatus.MOVED_PERMANENTLY)
                self.send_header('Location', '/diff/HEAD/WORK/F.Cu')
                self.end_headers()
                return

            if not url_parts.path.startswith('/'):
                self.send_error(http.HTTPStatus.NOT_FOUND)
                return

            path_parts = [urllib.parse.unquote(p) for p in url_parts.path[1:].split('/')]
            if len(path_parts) <= 1:
                self.send_error(http.HTTPStatus.NOT_FOUND)
                return

            #      0          1             2            3
            # /<action>/<base-commit>/<target-commit>/<object>
            # action: 'image', 'diff'
            # object: layer (F.Cu, Edge.Cuts, etc), 'sch', 'sch-<subsheet>'
            action = path_parts[0]
            if action not in ['image', 'diff'] or len(path_parts) != 4:
                self.send_error(http.HTTPStatus.NOT_FOUND)
                return

            if action == 'image':
                action_image(self, *path_parts[1:])
                return
            elif action == 'diff':
                action_diff(self, *path_parts[1:])
                return

            self.send_error(http.HTTPStatus.NOT_FOUND)
            return

        finally:
            logger.debug('do_GET end. path=%s traceid=%s', self.path, self.traceid)

def handler_factory(*f_args, **f_kwargs):
    def create(*args, **kwargs):
        return HTTPRequestHandler(*f_args, *args, **f_kwargs, **kwargs)
    return create

def find_kicad_pro_from_dir(dir_path):
    return next(dir_path.glob('*.kicad_pro'), None)

def determine_pcb_sch_from_pro(pro_path):
    pro_stem = pro_path.stem  # .kicad_pro を除いたファイル名
    def get_path(ext):
        p = pro_path.parent / (pro_stem + ext)
        return p if p.exists() else None

    return get_path('.kicad_pcb'), get_path('.kicad_sch')

def determine_pcb_sch(input_files):
    if len(input_files) == 0:
        return None, None
    elif len(input_files) == 1 and input_files[0].is_dir():
        pro_path = find_kicad_pro_from_dir(input_files[0])
        if pro_path is None:
            raise ValueError(f'kicad_pro file not found in the directory "{input_files[0]}"')
        return determine_pcb_sch_from_pro(pro_path)

    input_dir = input_files[0].parent
    for file in input_files[1:]:
        if input_dir != file.parent:
            raise ValueError('All input files must be in the same directory')

    pro_path = None
    pcb_path = None
    sch_path = None
    for file in input_files:
        if file.suffix == '.kicad_pro':
            pro_path = file
        elif file.suffix == '.kicad_pcb':
            pcb_path = file
        elif file.suffix == '.kicad_sch':
            sch_path = file

    if pro_path:
        p, s = determine_pcb_sch_from_pro(pro_path)
        if pcb_path is None:
            pcb_path = p
        if sch_path is None:
            sch_path = s

    return pcb_path, sch_path

def read_config(args):
    p = configparser.ConfigParser()
    if args.conf is None:
        p.read(Path(__file__).parents[2] / 'kidivis_sample.ini')
    else:
        p.read(args.conf)

    kicad_cli = p.get('common', 'kicad_cli', fallback='/mnt/c/Program Files/KiCad/9.0/bin/kicad-cli.exe')
    layers = p.get('common', 'layers')
    if layers is None:
        layers = ['.'.join(p) for p in product(['F', 'B'], ['Cu', 'Silkscreen', 'Mask'])] + ['Edge.Cuts']
    else:
        layers = layers.split()  # 空白区切り

    return {
        'common': {
            'kicad_cli': kicad_cli,
            'layers': layers,
        },
        'server': {
            'port': args.port or p.getint('server', 'port', fallback=8000),
            'host': args.host or p.get('server', 'host', fallback='0.0.0.0'),
            'log_level': args.log_level or p.get('server', 'log_level', fallback='info'),
        }
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, help='server port number')
    p.add_argument('--host', help='server host address')
    p.add_argument('--log-level', choices=LOG_LEVELS, help='change logging level')
    p.add_argument('--conf', help='configuration file')
    p.add_argument('files', nargs='+', help='A path to .kicad_pro/pcb/sch files or its directory')
    args = p.parse_args()

    kidivis_root = Path(__file__).parents[2]
    if args.conf is None:
        # kidivis.ini が存在すれば args.conf に設定
        default_conf_path = kidivis_root / 'kidivis.ini'
        if default_conf_path.exists():
            args.conf = default_conf_path

    conf = read_config(args)

    log_level = getattr(logging, conf['server']['log_level'].upper())
    logging.basicConfig(level=log_level, format='%(asctime)-15s %(levelname)s:%(name)s:%(message)s')

    pcb_path, sch_path = determine_pcb_sch([Path(f).absolute() for f in args.files])
    logger.info('conf="%s" pcb="%s" sch="%s"', args.conf, pcb_path, sch_path)

    kicad_proj_dir = (pcb_path or sch_path).parent
    git_repo = repo.Git(kicad_proj_dir)
    backups_repo = repo.Backups(kicad_proj_dir)
    kicad_repo = repo.Repo(git_repo, backups_repo)

    logger.info('kicad project directory: %s', kicad_proj_dir)

    host = conf['server']['host']
    port = conf['server']['port']
    kicad_cli = conf['common']['kicad_cli']
    layers = conf['common']['layers']

    access_host = 'localhost'
    if host != '0.0.0.0':
        access_host = host

    with tempfile.TemporaryDirectory(prefix='kidivis') as td:
        tmp_dir_path = Path(td)
        logger.info('temporary directory: %s', tmp_dir_path)

        jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(Path(__file__).parent / 'templates')),
            autoescape=jinja2.select_autoescape()
        )
        create_handler = handler_factory(tmp_dir_path, kicad_repo, jinja_env, pcb_path, sch_path, kicad_cli, layers)
        with http.server.HTTPServer((host, port), create_handler) as server:
            print(f'Serving HTTP on {host} port {port}'
                  + f' (http://{access_host}:{port}/) ...')
            server.serve_forever()

    sys.exit(0)


if __name__ == '__main__':
    main()
