"""有截断与版本作用域的合成场景目录。"""
import time
from source.common import *

class Site(Provider):
    def initial(self):
        records=[]
        for unit in ['east','west']:
            for i in range(9):
                records.append({'unit':unit,'id':str(i),'title':'需求'+str(i),'industries':['medical' if i%2 else 'robot','shared'],'year':2025+i%2,'status':'open','revision':1})
        return {'versions':{'east':1,'west':1},'records':records,'blocked':{},'events':[]}
    def event(self,d):
        unit=d.get('unit','east');kind=d['event']
        if kind=='throttle':self.state['blocked'][unit]=time.time()+d.get('seconds',2)
        elif kind in ['move','withdraw','backdate','remove','expire']:
            self.state['versions'][unit]+=1
            rows=self.state['records'];r=next(x for x in rows if x['unit']==unit and x['id']==d.get('id','0'))
            if kind=='move':r['industries']=['medical'];r['revision']+=1
            if kind=='withdraw':r['status']='withdrawn';r['revision']+=1
            if kind=='remove':rows.remove(r)
            if kind=='backdate':rows.append({**r,'id':'older-added','year':2024,'revision':1})
            self.state['events'].append({'unit':unit,'version':self.state['versions'][unit],'event':kind})
        else:return Reply({'error':'unknown_event'},400)
        return {'ok':True}
    def get(self,p,q,h):
        if p=='/':return web(page('场景目录来源','<p>授权检索入口 /catalog，单位、分类与合法筛选见 /facets。</p>'))
        if p=='/facets':return {'units':self.state['versions'],'industries':['medical','robot','shared'],'years':[2024,2025,2026],'cap':4,'page_size':2,'filters':['unit','industry','year']}
        if p=='/revisions':return self.state['events']
        if p=='/catalog':
            allowed={'unit','industry','year','version','page'}
            if set(q)-allowed:return Reply({'error':'unsupported_filter'},400)
            unit=q.get('unit','east');version=self.state['versions'][unit]
            if time.time()<self.state['blocked'].get(unit,0):return Reply({'error':'unit_quota','unit':unit},429,headers={'Retry-After':'2'})
            if q.get('version') and int(q['version'])!=version:return Reply({'error':'version_expired','current':version},409)
            rows=[r for r in self.state['records'] if r['unit']==unit and (not q.get('industry') or q['industry'] in r['industries']) and (not q.get('year') or r['year']==int(q['year']))]
            offset=int(q.get('page','0'))*2
            return {'unit':unit,'query':{k:v for k,v in q.items() if k not in ['page','version']},'version':version,'total':len(rows),'cap':4,'truncated':len(rows)>4,'items':rows[:4][offset:offset+2],'next':offset+2<min(4,len(rows))}
        return super().get(p,q,h)

if __name__=='__main__':main(Site)
