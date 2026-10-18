// Additional local-only scenario: same URL/title, asynchronous Workday-style majors.
document.querySelector('h1').textContent = 'Job Application';
const step = document.createElement('div');
step.setAttribute('aria-current', 'step'); step.textContent = 'current step 3 of 7 My Experience';
document.body.prepend(step);
function upgradeMajors() {
  document.querySelectorAll('input[id$="--fieldOfStudy"]').forEach(input => {
    if (input.dataset.upgraded) return;
    input.dataset.upgraded = 'true'; input.placeholder = 'Search'; input.setAttribute('enterkeyhint','search');
    const wrapper = document.createElement('div'); wrapper.dataset.automationId = 'multiSelectContainer';
    input.before(wrapper); wrapper.append(input);
    const selected = document.createElement('div'); wrapper.append(selected);
    function makePill(value) {
      const pill=document.createElement('div');pill.dataset.automationId='selectedItem';
      const label=document.createElement('span');label.textContent=value;pill.append(label);
      const remove=document.createElement('button');remove.type='button';remove.setAttribute('aria-label','Remove '+value);
      remove.dataset.automationId='DELETE_charm';remove.onclick=()=>pill.remove();pill.append(remove);
      return pill;
    }
    // Adobe-10 preselected a site label before the model saved any equivalence decision.
    if (input.id === 'education-2--fieldOfStudy') {
      selected.append(makePill('Computer and Information Science'));input.value='';
    }
    const popup = document.createElement('div'); popup.setAttribute('role','listbox');
    popup.dataset.automationId = 'activeListContainer'; popup.hidden = true; wrapper.append(popup);
    const options = window.majorOptions || ['Systems Science', 'Computer and Information Science', 'Accounting'];
    function show(values) {
      popup.replaceChildren(...values.map(value => {
        const row = document.createElement('div'); row.setAttribute('role','option'); row.textContent=value;
        row.onclick = () => {
          const pill=makePill(value);
          selected.replaceChildren(pill); input.value='';popup.hidden=true;
        }; return row;
      }));
    }
    input.onclick = () => { popup.hidden=false;show(options); };
    input.onkeydown = e => {
      if(e.key==='Escape'){e.preventDefault();popup.hidden=true;}
      if(e.key==='Enter'){e.preventDefault(); const query=input.value.toLowerCase();
        setTimeout(()=>show(options.filter(x=>x.toLowerCase().includes(query))),200);}
    };
    document.addEventListener('keydown', e => {
      if(e.key==='Escape') popup.hidden=true;
    });
    document.addEventListener('click', e => {
      if(!wrapper.contains(e.target)) popup.hidden=true;
    });
  });
}
upgradeMajors();
document.querySelector('#addSchool').onclick = () => { addSchool();upgradeMajors(); };
document.querySelector('form').onsubmit = e => {
  e.preventDefault();window.nextCount++;
  document.querySelector('form').remove();
  step.textContent='current step 4 of 7 Application Questions';
  const question=document.createElement('input'); question.id='eligibility';question.required=true;
  document.body.append(question);
};

// Verified upload status captured in Adobe-11; role=alert does not make success an error.
const upload=document.createElement('div');upload.dataset.automationId='attachments-FileUpload';
upload.innerHTML='<label for="local-cv">Resume/CV</label><input type="file" id="local-cv"><div data-automation-id="file-upload-item"><span data-automation-id="file-upload-item-name">resume.pdf</span><span data-automation-id="file-upload-successful">Attached</span></div><div role="alert">resume.pdf successfully uploaded</div>';
document.querySelector('form').append(upload);
