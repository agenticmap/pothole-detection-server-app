data=ACC09092017110122PMFinal;
%-----LinAccMax----
for i=1:length(data)
    v=[data(i,1),data(i,2),data(i,3)];
    Norm(i,10) = norm(v);
end
%-------GbarMax-----
Norm(:,1)=data(:,33);

for i=1:length(data)
    Norm(i,2) = data(i,32)/data(i,31);
end

%-------LinAccMin----
for i=1:length(data)
    v=[data(i,4),data(i,5),data(i,6)];
    Norm(i,12) = norm(v);
end
%-------LinMax-LinMin-----
for i=1:length(data)
    Norm(i,3) = Norm(i,1)-Norm(i,2);
end
%------GyroMax--------
for i=1:length(data)
    v=[data(i,7),data(i,8),data(i,9)];
    Norm(i,4) = norm(v);
end
%--------GyroMin--------
for i=1:length(data)
    v=[data(i,10),data(i,11),data(i,12)];
    Norm(i,5) = norm(v);
end
%-------AccMax---------
for i=1:length(data)
    v=[data(i,13),data(i,14),data(i,15)];
    Norm(i,6) = norm(v);
end
%------AccMin-------------
for i=1:length(data)
    v=[data(i,16),data(i,17),data(i,18)];
    Norm(i,7) = norm(v);
end
%------NormStd------
Norm(:,8)=data(:,31);
%-------PotMag------
Norm(:,9)=data(:,32);
%-------GbarMin-----
Norm(:,11)=data(:,34);
%----StdRatio--------
mapcaplot(Norm)
[coeff,score,latent] = pca(zscore(Norm));
biplot(coeff(:,1:2),'scores',score(:,1:2));

[~,score1] = pca(zscore(Norm),'NumComponents',2);
         options = statset('MaxIter',1000);

         gmfit = fitgmdist(score1([1:50],:) ,3,'Start','plus','Options',options);
         %gmm.mu(i,:)=gmfit.mu(i,:);
         %gmm.Sigma(:,:,i) = gmfit.Sigma(:,:,i);
         %gmm=gmfit;
         figure;
         %gmmfinal = gmdistribution(gmfit.mu, gmfit.Sigma);
         gmmfinal=gmfit;
         fcontour(@(x,y)pdf(gmmfinal,[x y]),[-1.5 3.5 -1.5 5],'Fill','on')
         figure;
         ezsurf(@(x,y)pdf(gmmfinal,[x y]),[-1.5 3.5 -1.5 5])     
         h1 = gscatter(score1(:,1),score1(:,2));
