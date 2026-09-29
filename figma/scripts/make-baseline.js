// Clone final 201 into a hidden baseline frame far off-canvas for later diffing
const page = figma.currentPage;
const fr = page.children.find(c => c.type === 'FRAME' && c.name.startsWith('201 · ') && c.name.includes('final') && !c.name.includes('baseline'));
const old = page.children.find(c => c.name.includes('201 · ') && c.name.includes('baseline')); if (old) old.remove();
const cl = fr.clone(); page.appendChild(cl); cl.name = fr.name + ' · baseline'; cl.x = -8000; cl.y = -3008; cl.visible = false; cl.locked = true;
return { baselineId: cl.id, nodes: cl.children.find(c => c.name === 'Чертёж').children.length };