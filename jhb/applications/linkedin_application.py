"""Exact Easy Apply job transport, independent of LinkedIn source navigation.

The modal runtime is registered/enabled only after its observed controls have
been reviewed. Recognizing a numeric LinkedIn URL does not enable Easy Apply.
"""
from .cli_browser import BrowserUseCLI
from .linkedin_runtime import linkedin_id


class LinkedInApplicationCLI(BrowserUseCLI):
    _dispatch_module = 'jhb.applications.linkedin_application_runtime'

    def __init__(self, job, **kwargs):
        super().__init__(**kwargs)
        job_id = linkedin_id(job.get('url'))
        if not job_id or job.get('board_type') != 'linkedin_easy_apply':
            raise ValueError('Easy Apply requires an observed capability and an exact LinkedIn job')
        if any(not isinstance(job.get(key), str) or not job[key].strip() or len(job[key]) > 512 for key in ('title','company')):
            raise ValueError('Easy Apply requires verified job title and company context')
        self.application_url = 'https://www.linkedin.com/jobs/view/'+job_id+'/'
        self._scope = {'board':'linkedin_easy_apply','job_id':job_id,'job_title':job['title'],'company':job['company']}

    def call(self, operation, *, _cancelled=None, **payload):
        payload['scope']=dict(self._scope)
        return super().call(operation,_cancelled=_cancelled,**payload)

    def allowed_url(self,url):
        return linkedin_id(url)==self._scope['job_id']

    async def open(self,url):
        if not self.allowed_url(url):
            raise ValueError('Requested LinkedIn job differs from the approved Easy Apply scope')
        response=await self.invoke('open',url=self.application_url)
        if not self.allowed_url(response.get('url')) or response.get('guarded') is not True:
            self.redirected_to=response.get('url')
            raise ValueError('Easy Apply job ownership or submission guard was not confirmed')
        self.target_id,self.expected_url=response['target_id'],response['url']
        return response

    async def ensure_education(self,count):
        # Education on LinkedIn's profile is not permission to change that
        # profile, and an application-specific repeater must first be observed.
        return {'supported':False,'reason':'Observe education controls inside the owned Easy Apply modal'}
