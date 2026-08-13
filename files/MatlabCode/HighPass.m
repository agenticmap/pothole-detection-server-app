Fpass = 10;
Fstop = 11.5;
Apass = 1;
Astop = 7;
Fs = 23;

d = designfilt('lowpassfir', ...
  'PassbandFrequency',Fpass,'StopbandFrequency',Fstop, ...
  'PassbandRipple',Apass,'StopbandAttenuation',Astop, ...
  'DesignMethod','equiripple','SampleRate',Fs);

fvtool(d)
