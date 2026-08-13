for i=1:length(GyroXmax)
    v=[AccXmax(i,1),AccYmax(i,1),AccZmax(i,1)];
    NormAccmax(i,1) = norm(v);
end
fftGyro = fft(NormAccmax);
