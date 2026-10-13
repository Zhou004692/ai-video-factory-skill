/* Keep media visible while reviewing content. Existing version guards own approval. */
(function(global){
  'use strict';
  const playback=new Map();
  function prepare(kicker){
    if(!['CONTENT REVIEW','HUMAN REVIEW','CANDIDATE REVIEW','MASTER REVIEW','MASTER CANDIDATE','NATIVE VIDEO REPAIR','LOCAL PICTURE RECUT'].includes(kicker))return;
    const body=document.querySelector('#modal-body'),form=body.querySelector('.form-stack'),video=form?.querySelector('video');
    for(const id of ['content-master','content-source']){const field=body.querySelector('#'+id);if(field)field.dataset.browseOnly='true'}
    body.querySelectorAll('video').forEach(media=>{
      let key=media.getAttribute('src')?.split('#')[0];if(!key)return;
      const identity=new URL(key,global.location.href);if(identity.searchParams.has('expected_sha256')){identity.searchParams.delete('v');key=identity.href}
      media.addEventListener('timeupdate',()=>playback.set(key,media.currentTime));
      media.addEventListener('loadedmetadata',()=>{const at=playback.get(key);if(Number.isFinite(at)&&at<media.duration)media.currentTime=at});
    });
    if(!form||!video)return;
    const grid=document.createElement('div');grid.className='review-workspace-grid';
    const player=document.createElement('aside');player.className='review-player';
    const caption=document.createElement('p');caption.className='review-player-caption';caption.textContent='当前审核素材 · 在播放器中定位问题区间';player.append(video,caption);
    const details=document.createElement('div');details.className='review-details';while(form.firstChild)details.append(form.firstChild);
    grid.append(player,details);form.append(grid);
    details.querySelectorAll('[data-content-id]').forEach(section=>{
      const start=section.querySelector('[data-content-start]'),end=section.querySelector('[data-content-end]');
      if(!start)return;
      const button=document.createElement('button');button.type='button';button.className='mini-action';button.textContent='播放证据区间';button.onclick=()=>{
        const a=Number(start.value),b=Number(end?.value),duration=Number(video.duration);
        if(!Number.isFinite(duration)||start.value===''||!Number.isFinite(a)||a<0||a>=duration||end?.value===''||!Number.isFinite(b)||b<=a||b>duration){caption.textContent='请先加载素材，并填写时长范围内有效的开始与结束秒。';return}
        video.currentTime=a;caption.textContent=`当前证据区间 ${a}–${b} 秒`;
        if(video._stopEvidence)video.removeEventListener('timeupdate',video._stopEvidence);
        const stop=()=>{if(video.currentTime>=b){video.pause();video.removeEventListener('timeupdate',stop);video._stopEvidence=null}};video._stopEvidence=stop;
        video.addEventListener('timeupdate',stop);video.play().catch(()=>{caption.textContent='播放未开始，请使用播放器播放按钮。'});
      };section.querySelector('h3')?.after(button);
    });
    video.addEventListener('error',()=>{caption.textContent='素材加载失败，不能确认可播放；请重新读取或查看素材恢复入口。'});
  }
  global.AVFReviewWorkspace=Object.freeze({prepare});
})(globalThis);
