from re import findall 
from math import floor
from time import time
from os import path as ospath
from aiofiles import open as aiopen
from aiofiles.os import remove as aioremove, rename as aiorename
from asyncio import sleep as asleep, gather, create_subprocess_shell, create_task
from asyncio.subprocess import PIPE

from bot import Var, bot_loop, ffpids_cache, LOGS
from .func_utils import mediainfo, convertBytes, convertTime, sendMessage, editMessage
from .reporter import rep

# Use GPU_TYPE from Var
GPU_TYPE = getattr(Var, "GPU_TYPE", "cpu")  # Defaults to CPU if not set

ffargs = {
    'nvidia': {
        '1080': "ffmpeg -hwaccel cuda -i '{}' -c:v h264_nvenc -preset slow -b:v 5M -c:a copy -progress {} '{}'",
        '720': "ffmpeg -hwaccel cuda -i '{}' -c:v h264_nvenc -preset slow -b:v 3M -c:a copy -progress {} '{}'",
        '480': "ffmpeg -hwaccel cuda -i '{}' -c:v h264_nvenc -preset slow -b:v 1.5M -c:a copy -progress {} '{}'",
        '360': "ffmpeg -hwaccel cuda -i '{}' -c:v h264_nvenc -preset slow -b:v 1M -c:a copy -progress {} '{}'",
    },
    'intel': {
        '1080': "ffmpeg -hwaccel vaapi -i '{}' -vf format=nv12,hwupload -c:v h264_vaapi -b:v 5M -c:a copy -progress {} '{}'",
        '720': "ffmpeg -hwaccel vaapi -i '{}' -vf format=nv12,hwupload -c:v h264_vaapi -b:v 3M -c:a copy -progress {} '{}'",
        '480': "ffmpeg -hwaccel vaapi -i '{}' -vf format=nv12,hwupload -c:v h264_vaapi -b:v 1.5M -c:a copy -progress {} '{}'",
        '360': "ffmpeg -hwaccel vaapi -i '{}' -vf format=nv12,hwupload -c:v h264_vaapi -b:v 1M -c:a copy -progress {} '{}'",
    },
    'cpu': {
        '1080': "ffmpeg -i '{}' -c:v libx264 -preset slow -b:v 5M -c:a copy -progress {} '{}'",
        '720': "ffmpeg -i '{}' -c:v libx264 -preset slow -b:v 3M -c:a copy -progress {} '{}'",
        '480': "ffmpeg -i '{}' -c:v libx264 -preset slow -b:v 1.5M -c:a copy -progress {} '{}'",
        '360': "ffmpeg -i '{}' -c:v libx264 -preset slow -b:v 1M -c:a copy -progress {} '{}'",
    }
}

class FFEncoder:
    def __init__(self, message, path, name, qual):
        self.__proc = None
        self.is_cancelled = False
        self.message = message
        self.__name = name
        self.__qual = qual
        self.dl_path = path
        self.__total_time = None
        self.out_path = ospath.join("encode", name)
        self.__prog_file = 'prog.txt'
        self.__start_time = time()

    async def progress(self):
        self.__total_time = await mediainfo(self.dl_path, get_duration=True)
        if isinstance(self.__total_time, str) or not self.__total_time:
            self.__total_time = 1.0  # Avoid division errors
        
        while not (self.__proc is None or self.is_cancelled):
            async with aiopen(self.__prog_file, 'r+') as p:
                text = await p.read()
            
            if text:
                time_done = floor(int(t[-1]) / 1000000) if (t := findall("out_time_ms=(\d+)", text)) else 1
                ensize = int(s[-1]) if (s := findall(r"total_size=(\d+)", text)) else 0
                
                diff = time() - self.__start_time
                speed = ensize / max(diff, 0.01)
                percent = round((time_done / self.__total_time) * 100, 2)
                tsize = ensize / (max(percent, 0.01) / 100)
                eta = (tsize - ensize) / max(speed, 0.01)
    
                bar = floor(percent / 8) * "█" + (12 - floor(percent / 8)) * "▒"
                
                progress_str = f"""<blockquote>‣ <b>Anime Name :</b> <b><i>{self.__name}</i></b></blockquote>
<blockquote>‣ <b>Status :</b> <i>Encoding</i>
    <code>[{bar}]</code> {percent}%</blockquote> 
<blockquote>   ‣ <b>Size :</b> {convertBytes(ensize)} out of ~ {convertBytes(tsize)}
    ‣ <b>Speed :</b> {convertBytes(speed)}/s
    ‣ <b>Time Took :</b> {convertTime(diff)}
    ‣ <b>Time Left :</b> {convertTime(eta)}</blockquote>
<blockquote>‣ <b>File(s) Encoded:</b> <code>{Var.QUALS.index(self.__qual) if self.__qual in Var.QUALS else '?'} / {len(Var.QUALS)}</code></blockquote>"""
            
                await editMessage(self.message, progress_str)
                if (prog := findall(r"progress=(\w+)", text)) and prog[-1] == 'end':
                    break
            await asleep(8)
    
    async def start_encode(self):
        if ospath.exists(self.__prog_file):
            await aioremove(self.__prog_file)
    
        async with aiopen(self.__prog_file, 'w+'):
            LOGS.info("Progress Temp Generated !")
        
        dl_npath, out_npath = ospath.join("encode", "ffanimeadvin.mkv"), ospath.join("encode", "ffanimeadvout.mkv")
        await aiorename(self.dl_path, dl_npath)
        
        # Choose correct FFmpeg command based on GPU type
        if GPU_TYPE not in ffargs:
            LOGS.error(f"Invalid GPU_TYPE: {GPU_TYPE}. Defaulting to CPU.")
            ffcode = ffargs["cpu"][self.__qual].format(dl_npath, self.__prog_file, out_npath)
        else:
            ffcode = ffargs[GPU_TYPE][self.__qual].format(dl_npath, self.__prog_file, out_npath)

        LOGS.info(f'FFCode: {ffcode}')
        self.__proc = await create_subprocess_shell(ffcode, stdout=PIPE, stderr=PIPE)
        proc_pid = self.__proc.pid
        ffpids_cache.append(proc_pid)
        _, return_code = await gather(create_task(self.progress()), self.__proc.wait())
        ffpids_cache.remove(proc_pid)
        
        await aiorename(dl_npath, self.dl_path)
        
        if self.is_cancelled:
            return
        
        if return_code == 0:
            if ospath.exists(out_npath):
                await aiorename(out_npath, self.out_path)
            return self.out_path
        else:
            await rep.report((await self.__proc.stderr.read()).decode().strip(), "error")
            
    async def cancel_encode(self):
        self.is_cancelled = True
        if self.__proc is not None:
            try:
                self.__proc.kill()
            except:
                pass
